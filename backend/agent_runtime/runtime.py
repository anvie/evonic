"""
runtime.py — AgentRuntime orchestrator.

Owns: message queue, worker threads, session locks, skill caches, handle_message,
session lifecycle, agent state management. Delegates heavy lifting to context,
llm_loop, and summarizer.
"""
from __future__ import annotations

import logging
import mimetypes
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
from models.boolean import message_wrapper_enabled
from models.chatlog import chatlog_manager
from config import AGENT_TIMEOUT_RETRIES as MAX_TIMEOUT_RETRIES, AGENT_QUEUE_WORKERS

from backend.agent_runtime import context as _ctx
from backend.agent_runtime import llm_loop as _loop
from backend.agent_runtime import summarizer as _sum
from backend.agent_runtime.concurrency import ConcurrencyManager
from backend.agent_state import AgentState
from backend.channels.registry import channel_manager
from backend.channels.base import BaseChannel
from backend.event_stream import event_stream
from backend.plugin_manager import get_busy_message
from backend.slash_commands import parse_command, execute_command
from backend.agent_runtime.prefetch import TurnPrefetcher
import atexit
import json
import re
from config import AGENT_MAX_TOOL_RESULT_CHARS as MAX_TOOL_RESULT_CHARS
from config import STALE_SESSION_INJECTION_ENABLED, STALE_SESSION_THRESHOLD_SECONDS
from config import LONG_GAP_WEEKS, BACKGROUND_JOBS_INJECTION_ENABLED

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOGS_DIR = os.path.join(_BASE_DIR, 'logs')

_logger = logging.getLogger(__name__)

# Image-embed detection for external channels (web renders these fine; chat
# clients like WhatsApp/Telegram/Discord do not). Used by the final-message
# safety net in AgentRuntime._strip_media_embeds().
_IMG_EMBED_RE = re.compile(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\'][^>]*>', re.IGNORECASE)
_MD_IMG_EMBED_RE = re.compile(r'!\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
_ARTIFACT_URL_RE = re.compile(r'^/api/agents/([^/]+)/artifacts/(.+)$')
_ATTACHMENT_URL_RE = re.compile(r'^/api/attachments/(\d+)(?:/view)?$')


def _append_attachment_context(content: str, attachment_infos, attachment_info,
                               agent: dict, has_describe_image: bool) -> str:
    """Append context notes for plural attachments with a legacy singular fallback."""
    if not isinstance(attachment_infos, list):
        attachment_infos = []
    attachment_infos = [info for info in attachment_infos if isinstance(info, dict)]
    if not attachment_infos and isinstance(attachment_info, dict):
        attachment_infos = [attachment_info]

    notes = []
    for index, info in enumerate(attachment_infos, 1):
        fp = info.get('file_path', '')
        if fp and not os.path.isabs(fp):
            fp = os.path.abspath(os.path.join(_BASE_DIR, fp))
        fn = info.get('filename', '')
        mt = info.get('mime_type', '')
        sb = int(info.get('size_bytes', 0) or 0)
        is_img = bool(mt and mt.startswith('image/'))
        is_audio = bool(mt and mt.startswith('audio/'))
        if sb >= 1048576:
            sz = f"{sb / 1048576:.1f} MB"
        elif sb >= 1024:
            sz = f"{sb / 1024:.1f} KB"
        else:
            sz = f"{sb} B"
        label = f"Attachment #{index}" if len(attachment_infos) > 1 else "Attachment"
        note = f"\n\n[{label}: {fn} ({mt}, {sz})]\nFile path: {fp}"
        if is_img and has_describe_image:
            note += "\nUse the `describe_image` tool to view and analyze this image."
        if is_audio and agent.get('audio_enabled'):
            note += "\nUse the `transcribe_audio` tool to listen to this audio."
        notes.append(note)
    return content.rstrip() + ''.join(notes) if notes else content


# --- Configuration constants ---
CLEANUP_INTERVAL_SECONDS = 300       # Interval between idle session cleanup sweeps (5 minutes)
CLEANUP_TTL_SECONDS = 3600          # TTL for session state entries before cleanup (1 hour)
WORKER_JOIN_TIMEOUT_SECONDS = 5.0   # Max time to wait for worker threads to finish on shutdown
WORKER_JOIN_MIN_TIMEOUT = 0.1       # Minimum timeout per worker join iteration (seconds)
DEFAULT_BUFFER_SECONDS = 2          # Default message buffering delay when agent has no config (seconds)
SESSION_BUFFER_CLEANUP_DELAY = 30.0 # Delay before cleaning up SSE session buffers (seconds)


def _llm_log_path(agent_id: str) -> str:
    return os.path.join(_LOGS_DIR, 'agents', agent_id, 'llm.log')


# Message wrapper prefix prepended to user messages when enabled.
# Always in English regardless of user language, max 60 tokens.
WRAPPER_PREFIX = (
        """[Pre-response check: Before you answer or reply to the user's request, first verify the following:

1. Does the message contain explicit or implicit information—such as instructions, or shared personal details, facts, preferences, phone numbers, secret keys, addresses, PINs, etc.? If yes, save it to memory using the `remember()` tool. If the message contains style notes, rules, or procedures that don't always need to be applied, record them via `remember()` for non-factual style notes. If the message contains a request to change style or rules that is highly critical and must be applied on every turn, write it to `SYSTEM.md`.
2. Does the message relate to a specific project or task? If so, read the relevant knowledge base files first—there may be an existing KB containing per-item/per-project procedures.
3. Only after completing these steps, reply to the user naturally.]

The following is the message from the user:\n
----begin-user-message----\n"""
)

SUBAGENT_EXECUTE_DIRECTIVE = (
    "You are running as a sub-agent on a delegated task. Plan/approval cycles are "
    "disabled for you. Do not produce a plan or wait for approval — execute the task "
    "directly and run to completion, then report your final result."
)


def _apply_restart_origin_guard(agent_context: dict, assigned_tool_ids: list,
                                tools: list, metadata: dict) -> tuple:
    """Remove restart authorization from an automatic post-restart turn."""
    if not isinstance(metadata, dict) or not metadata.get('restart_origin'):
        return assigned_tool_ids, tools
    agent_context['restart_origin'] = True
    assigned_tool_ids = [
        tool_id for tool_id in assigned_tool_ids
        if tool_id != 'restart' and not tool_id.endswith(':restart')
    ]
    tools = [
        tool for tool in tools
        if tool.get('function', {}).get('name') != 'restart'
    ]
    agent_context['assigned_tool_ids'] = assigned_tool_ids
    return assigned_tool_ids, tools


def _should_wrap_user_message(agent: dict) -> bool:
    """Check if message wrapper is enabled for this agent.

    Priority: per-agent setting > global setting > default (True).
    """
    return message_wrapper_enabled(agent, db)


def _apply_wrapper_prefix(messages: list, enabled: bool,
                          is_stale: bool = False,
                          stale_threshold: int = 25200,
                          loaded_skills_count: int = 0) -> None:
    """Apply message wrapper prefix to user messages in-place.

    Wraps: (a) the LAST (current) user message when it has >= 4 words,
           (b) historical messages explicitly marked with _wrapped=True.
    Cleans up the _wrapped key after use.

    When is_stale is True, appends a staleness-awareness note to the wrapper
    prefix prompting the agent to assess whether conversation history is
    still relevant.

    When loaded_skills_count > 0, replaces point 3 with a dynamic reminder
    to check loaded skills before responding.
    """
    if not enabled or not messages:
        return

    # Build effective wrapper prefix — may include staleness notice or skill reminder
    effective_prefix = WRAPPER_PREFIX
    if is_stale:
        hours = max(1, stale_threshold // 3600)
        staleness_note = (
            f"[STALENESS NOTICE] The previous message in this session was sent "
            f"more than {hours} hour{'s' if hours != 1 else ''} ago. "
            f"Consider whether the conversation history is still relevant. "
            f"If topics have shifted, suggest the user run /clear to reset "
            f"context.\n\n"
        )
        effective_prefix = WRAPPER_PREFIX + staleness_note

    # Dynamic point 3: if agent has loaded skills, occasionally remind it to check/unload them
    # 1:3 probability — injects ~25% of the time to avoid redundancy
    if loaded_skills_count and loaded_skills_count > 0:
        import random
        if random.random() < 0.25:
            options = [
                f"[SKILL — {loaded_skills_count} loaded] Use a relevant one if this fits. Otherwise unload it — it's no longer needed.",
                f"[SKILL — {loaded_skills_count} loaded] Check if any loaded skill applies to this request. If not, unload it before replying.",
                f"[SKILL — {loaded_skills_count} loaded] Got skills loaded. Use one if relevant, or unload it if it's stale. Then reply naturally.",
                f"[SKILL — {loaded_skills_count} loaded] Loaded skills ready. Pick the right one or unload the irrelevant ones. Then respond.",
            ]
            effective_prefix = WRAPPER_PREFIX + f"\n\n{random.choice(options)}"
    else:
        if is_stale:
            effective_prefix = WRAPPER_PREFIX + staleness_note

    for i, msg in enumerate(messages):
        if msg.get('role') == 'user':
            _wrapped = msg.pop('_wrapped', None)
            is_current = (i == len(messages) - 1)
            if is_current or _wrapped:
                content = msg['content']
                # Handle multimodal content (list of parts) where the text
                # lives inside a part dict instead of as a plain string.
                if isinstance(content, list):
                    _text_part_idx = next(
                        (j for j, p in enumerate(content)
                         if isinstance(p, dict) and p.get('type') == 'text'), None)
                    _text = content[_text_part_idx]['text'] if _text_part_idx is not None else ''
                    # Skip wrapper injection for short current-turn messages
                    if is_current and len(_text.split()) < 4:
                        continue
                    if _text_part_idx is not None:
                        content[_text_part_idx]['text'] = effective_prefix + _text
                else:
                    # Plain string content
                    # Skip wrapper injection for short current-turn messages
                    # (fewer than 4 words).  Messages like "ok", "thanks", "yes"
                    # contain no implicit preferences worth scanning for, so the
                    # wrapper would waste tokens.
                    if is_current and len(content.split()) < 4:
                        continue
                    msg['content'] = effective_prefix + content


def _build_long_gap_context(chatlog, gap_weeks: int) -> Optional[str]:
    """Build long-absence context when user hasn't messaged in >= gap_weeks.

    Returns a formatted system context string for injection, or None if the
    gap is below threshold or no user entries exist.
    """
    if gap_weeks <= 0:
        return None
    entry = chatlog.get_last_entry(types=frozenset({'user'}))
    if not entry:
        return None
    last_ts = entry.get('ts', 0)
    if not last_ts:
        return None
    elapsed_seconds = time.time() - (last_ts / 1000.0)
    gap_seconds = gap_weeks * 7 * 24 * 3600
    if elapsed_seconds < gap_seconds:
        return None

    weeks = int(elapsed_seconds // (7 * 24 * 3600))
    days = int((elapsed_seconds % (7 * 24 * 3600)) // (24 * 3600))
    from datetime import datetime
    last_date = datetime.fromtimestamp(last_ts / 1000.0).strftime('%d %B %Y')

    duration_str = f"{weeks} week{'s' if weeks > 1 else ''}"
    if days > 0:
        duration_str += f" and {days} day{'s' if days > 1 else ''}"

    return (
        "## Long Absence Notice\n"
        f"This user has not sent a message in over {duration_str} "
        f"(last message: {last_date}).\n"
        "Be aware that they may need context recap, reminders about prior topics, "
        "or a gentle catch-up rather than assuming they remember all previous details.\n"
        "Use a courteous and understanding tone."
    )


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
        # sys.exit() only raises SystemExit in the main thread. When the signal
        # arrives while the main thread is blocked in the threaded WSGI server's
        # accept loop, that SystemExit gets swallowed by the server — the runtime
        # ends up drained (queue workers + executor stopped) but the process
        # stays alive serving requests. systemd still sees it as active, so
        # Restart=always never fires and the FD-watchdog's SIGTERM produces a
        # half-dead "zombie" instead of a restart. Arm a daemon backstop that
        # force-exits if the clean exit doesn't take, then attempt the clean
        # exit (which runs atexit Docker cleanup) first.
        forcer = threading.Timer(
            WORKER_JOIN_TIMEOUT_SECONDS + 3.0, lambda: os._exit(0)
        )
        forcer.daemon = True
        forcer.start()
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
                    # DIAGNOSTIC (shared-channel reply loss): confirm the buffered
                    # worker resolves a running channel instance and attempts the send.
                    _logger.info(
                        "[buffered-send] channel_id=%s instance=%s running=%s user=%s session=%s",
                        task.ctx.channel_id,
                        type(instance).__name__ if instance else None,
                        getattr(instance, 'is_running', None) if instance else None,
                        task.ctx.external_user_id, task.ctx.session_id)
                    if instance and instance.is_running:
                        try:
                            _out_text = result['response']
                            # External channels do not render HTML <img>/Markdown image
                            # embeds: deliver resolvable media via send_file and strip
                            # the embed markup from the outgoing text.
                            _out_text = self._strip_media_embeds(_out_text, task.ctx.session_id)
                            instance.send_message(task.ctx.external_user_id, _out_text)
                            # Check for async send errors (channel records failures internally)
                            if isinstance(instance, BaseChannel) and instance.has_send_error(task.ctx.external_user_id):
                                send_err = instance.get_send_error(task.ctx.external_user_id)
                                if send_err:
                                    _logger.warning(
                                        "Channel send error for session %s user %s: %s",
                                        task.ctx.session_id, task.ctx.external_user_id, send_err,
                                    )
                        except Exception as e:
                            _logger.error("Channel send error for session %s: %s", task.ctx.session_id, e)
                elif task.send_via_channel:
                    # DIAGNOSTIC (shared-channel reply loss): the reply was generated
                    # and saved (so it shows in the web session) but the channel send
                    # was skipped. Log exactly which precondition failed.
                    _logger.warning(
                        "[buffered-send] SKIPPED for session %s user=%s: "
                        "resp_empty=%s resp_placeholder=%s channel_id=%s",
                        task.ctx.session_id, task.ctx.external_user_id,
                        not bool(_resp), _resp == "(No response)", task.ctx.channel_id)
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

    def is_stop_requested(self, session_id: str) -> bool:
        """True if /stop was signalled for this session and not yet consumed.

        Public read-only view of the stop flag, for blocking tools (e.g. the
        sync Explore wait) that must abort promptly instead of holding the
        agent loop until their own timeout expires."""
        return self._get_stop_event(session_id).is_set()

    def summarize_session(self, agent: dict, session_id: str) -> bool:
        """Trigger summarization for a session. Public API for slash commands.

        Returns True if a summary was actually generated and persisted."""
        return _sum.maybe_summarize(
            agent, session_id,
            self._llm_serializer._summarize_guard,
            self._llm_serializer._summarize_active,
            self._llm_serializer._llm_lock,
        )

    def _run_bash_exec(self, agent: Dict[str, Any], session_id: str,
                       db_agent_id: str, external_user_id: str,
                       message: str) -> str:
        """Run a web user's "!<command>" directly and persist it for UI display only.

        The command and its output are saved with a `bash_exec` metadata flag so
        they render in the web chat (and survive reload) but are filtered out of
        the LLM context — the agent never sees them. Safety checks are bypassed
        (`_skip_safety`): the command is typed explicitly by a human operator and
        gated by the per-agent toggle.
        """
        cmd = message.lstrip()[1:].strip()
        if not cmd:
            return "Usage: `!<command>` — run a shell command directly (web only)."

        from backend.tools import bash
        exec_agent = {**agent, 'session_id': session_id, '_skip_safety': True}
        result = bash.execute(exec_agent, {'script': cmd})

        # Format the output as a fenced block for monospace rendering in the UI.
        if result.get('error'):
            body = result['error']
        else:
            parts = []
            stdout = (result.get('stdout') or '').rstrip('\n')
            stderr = (result.get('stderr') or '').rstrip('\n')
            if stdout:
                parts.append(stdout)
            if stderr:
                parts.append(stderr)
            body = "\n".join(parts) if parts else "(no output)"
        # Truncate output if it exceeds MAX_OUTPUT characters (both stdout+stderr).
        MAX_OUTPUT = 1000
        if len(body) > MAX_OUTPUT:
            body = body[:MAX_OUTPUT] + "\n… (truncated)"
        response = f"$ {cmd}\n```bash\n{body}\n```"
        exit_code = result.get('exit_code')
        if exit_code not in (0, None):
            response += f"\n_(exit code {exit_code})_"

        # Persist for UI display only — hidden from LLM via the `bash_exec` flag.
        _db_retry(db.add_chat_message, session_id, 'user', message,
                  agent_id=db_agent_id, metadata={'bash_exec': True},
                  label="save bash command")
        _db_retry(db.add_chat_message, session_id, 'assistant', response,
                  agent_id=db_agent_id, metadata={'bash_exec': True},
                  label="save bash output")
        _cl = chatlog_manager.get(db_agent_id, session_id)
        _cl.append({'type': 'user', 'session_id': session_id, 'content': message,
                    'sender_id': external_user_id, 'metadata': {'bash_exec': True}})
        _cl.append({'type': 'system', 'session_id': session_id, 'content': response,
                    'metadata': {'bash_exec': True}})
        self._prefetcher.invalidate(session_id)
        return response

    def handle_message(self, agent_id: str, external_user_id: str,
                       message: str, channel_id: Optional[str] = None,
                       image_url: Optional[str] = None,
                       audio_url: Optional[str] = None,
                       video_url: Optional[str] = None,
                       metadata: Optional[Dict[str, Any]] = None,
                       skip_buffer: bool = False,
                       session_id: Optional[str] = None) -> Dict[str, Any]:
        """Process an incoming user message. Always queued for processing.

        - With buffer: debounce rapid messages, queue when timer fires.
        - Without buffer: queue immediately and wait for result.

        Args:
            image_url: Optional base64 data URL or http URL for vision-enabled agents.
            audio_url: Optional base64 data URL for audio-enabled agents.
            video_url: Optional base64 data URL for video-enabled agents.
            metadata: Optional extra metadata merged into the saved message record.
            skip_buffer: If True, bypass message buffering even if the agent has
                message_buffer_seconds set. Used by API routes that need a synchronous
                response (e.g. /chat/completions).
            session_id: Existing owned session to use instead of resolving one from
                external_user_id and channel_id.
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

        # Defense-in-depth: check if the user is blocked (FINDING-012).
        # Skip internal/system users (they have no user record to check).
        if external_user_id not in ('__system__', '') and not external_user_id.startswith('__agent__'):
            if db.is_user_blocked_by_external_id(external_user_id):
                _logger.info(
                    "[handle_message] agent=%s sender=%s BLOCKED — rejecting message.",
                    agent_id, external_user_id,
                )
                return {
                    "response": (
                        "Your account has been suspended. "
                        "Please contact the administrator for further information."
                    ),
                    "tool_trace": [],
                }

        # Determine if this is a sub-agent (uses parent's per-agent chat DB)
        is_subagent = agent.get('is_subagent', False)

        # Get or create session (sub-agents store their own ID but use parent's DB)
        if session_id:
            session = _db_retry(db.get_session_with_details, session_id,
                                label="get explicit session")
            if not session or session.get('agent_id') != agent_id:
                return {"response": "Session not found.", "tool_trace": []}
            external_user_id = session.get('external_user_id') or external_user_id
            channel_id = session.get('channel_id')
        else:
            session_id = _db_retry(db.get_or_create_session, agent_id, external_user_id,
                                   channel_id, db_agent_id=db_agent_id if is_subagent else None,
                                   label="get/create session")

        # Sub-agents always start fresh — clear any stale messages from a
        # previous spawn that reused the same session slug.
        if is_subagent and metadata and metadata.get('subagent_spawn'):
            db.clear_session(session_id, agent_id=db_agent_id)

        # Web-only "!" direct bash execution — eval'd directly, bypassing the LLM.
        # Gated on the per-agent bash_exec_enabled flag and web channel (no channel_id).
        # On a channel, a "!"-prefixed message falls through as ordinary user text.
        if message.lstrip().startswith('!') and channel_id is None \
                and agent.get('bash_exec_enabled'):
            response = self._run_bash_exec(
                agent, session_id, db_agent_id, external_user_id, message,
            )
            return {"response": response, "tool_trace": [], "timeline": [],
                    "slash_command": True, "bash_exec": True}

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
        meta = {}
        if image_url:
            meta["image_url"] = image_url
        if audio_url:
            meta["audio_url"] = audio_url
        if video_url:
            meta["video_url"] = video_url
        if metadata:
            meta.update(metadata)
        # Flag user message as wrapped if preference wrapper is enabled for this agent
        if _should_wrap_user_message(agent):
            meta['wrapped'] = True
        # Enrich metadata and handle inter-agent clearing
        if external_user_id.startswith("__agent__"):
            # Clear session context before saving the new message when
            # inter_agent_clear_context is enabled for this agent.
            # This ensures the agent processes the message with a fresh
            # context containing only the sender's message.
            # Must run BEFORE the not meta.get('agent_message') guard because
            # send_agent_message already sets agent_message=True in metadata.
            if agent.get('inter_agent_clear_context'):
                # Skip clearing if the last message in this session arrived
                # less than 7 seconds ago — the sending agent may be sending
                # multiple messages concurrently.
                last_ts = db.get_last_message_timestamp(session_id, agent_id=db_agent_id)
                if last_ts and (time.time() - last_ts) < 7:
                    _logger.debug(
                        "Skipped inter-agent clear for session %s: "
                        "last message was %.1fs ago (within 7s window)",
                        session_id, time.time() - last_ts)
                else:
                    db.clear_session(session_id, agent_id=db_agent_id)
                    # Reset fallback model so the agent starts fresh with
                    # its primary model on the next inter-agent message.
                    try:
                        _as_raw = db.get_agent_state(db_agent_id)
                        _as = json.loads(_as_raw) if _as_raw else {}
                        if _as.pop('active_fallback_model_id', None):
                            db.upsert_agent_state(
                                json.dumps(_as), agent_id=db_agent_id)
                            _logger.info(
                                "Reset fallback model for agent %s after "
                                "inter-agent clear", db_agent_id)
                    except Exception:
                        pass
            if not meta.get('agent_message'):
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
            'audio_url': audio_url,
            'video_url': video_url,
            'metadata': meta,
        })

        # A plain human reply in the exact originating session resumes the
        # delegated agent that requested it. Save and emit it above for complete
        # history, then stop the originating agent from processing it as a new turn.
        is_human_message = not external_user_id.startswith(
            ('__agent__', '__system__', '__scheduler__'))
        if is_human_message and not meta.get('escalation_reply'):
            from backend.escalation_routing import route_pending_escalation_reply
            escalation_result = route_pending_escalation_reply(
                agent_id, session_id, message,
            )
            if escalation_result is not None:
                routed = bool(escalation_result.get('success'))
                return {
                    "response": (
                        "Your response was sent to the requesting agent."
                        if routed else
                        "Your response could not be sent to the requesting agent."
                    ),
                    "tool_trace": [],
                    "timeline": [],
                    "escalation_routed": routed,
                }

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
                        if isinstance(instance, BaseChannel) and instance.has_send_error(external_user_id):
                            err = instance.get_send_error(external_user_id)
                            _logger.warning(
                                "Channel send error for busy ack (session %s): %s",
                                session_id, err,
                            )
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
                          send_via_channel=bool(channel_id))
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
        db_agent_id = agent.get('_db_agent_id', agent_id)
        AgentRuntime._touch_session(ctx.session_id)
        try:
            if agent.get('is_explorer'):
                from backend.agent_runtime import explorer as _explorer
                model = _explorer.primary_model(agent) or db.get_agent_model(db_agent_id)
            else:
                model = db.get_agent_model(db_agent_id)
            model_id = model.get('id') if model else None
        except Exception as e:
            _logger.warning("Failed to resolve turn model for agent %s, proceeding without model gating: %s", agent_id, e)
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
        """Return a completed turn result dict if the agent's Tunnel Workplace is offline,
        otherwise return None so normal processing continues."""
        workplace_id = agent.get('workplace_id')
        if not workplace_id:
            return None
        try:
            workplace = db.get_workplace(workplace_id)
            if not workplace or workplace.get('type') != 'tunnel':
                return None
            from backend.workplaces.manager import workplace_manager
            status = workplace_manager.get_status(workplace_id)
            _logger.debug("Evonet status check for workplace=%s: %s", workplace_id, status.get('status'))
            if status.get('status') == 'connected':
                return None
        except Exception:
            _logger.warning("Failed to check Evonet status for workplace=%s, letting request through", workplace_id, exc_info=True)
            return None  # if we can't determine, let it proceed normally

        workplace_name = workplace.get('name', 'Tunnel Workplace')
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
                    if isinstance(instance, BaseChannel) and instance.has_send_error(ctx.external_user_id):
                        send_err = instance.get_send_error(ctx.external_user_id)
                        if send_err:
                            _logger.warning(
                                "Channel send error for evonet offline reply (session %s): %s",
                                ctx.session_id, send_err,
                            )
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
        from backend.tools.lib.process_tracker import process_tracker
        process_tracker.clear_stop(ctx.session_id)

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

        # Early rejection: if the agent has a tunnel Workplace and Evonet is offline,
        # reply immediately without hitting the LLM.
        _early_reply = self._check_evonet_offline(agent, ctx)
        if _early_reply:
            return _early_reply

        # ── CMP early boundary hook (offload-aware) ──────────────────────────
        # Runs BEFORE context assembly so a switch/branch decision shapes this
        # turn's history window, and gates the prefetch hit (prefetch builds
        # under the continue assumption). State is persisted immediately so
        # _restore_agent_state below re-reads consistent post-switch fields.
        # Rule 0 in the detector guarantees approval-like messages classify
        # `continue`, so the plan-approval check later is never bypassed.
        _cmp_early_handled = False
        _cmp_filter_state = None
        _cmp_switched_this_turn = False
        if (agent.get('enable_cmp') and agent.get('enable_agent_state')
                and not agent.get('is_subagent')):
            try:
                _cmp_ms = self._restore_agent_state(db_agent_id, session_id=ctx.session_id)
                if _cmp_ms is not None:
                    from backend.agent_runtime import cmp as _cmp_pkg
                    _cmp_chatlog = chatlog_manager.get(db_agent_id, ctx.session_id)
                    _cmp_user = _cmp_chatlog.get_last_entry(types=frozenset({'user'}))
                    _cmp_res = _cmp_pkg.on_turn_boundary(
                        agent, _cmp_ms, _cmp_chatlog,
                        (_cmp_user or {}).get('content', ''),
                        session_id=ctx.session_id, agent_id=agent['id'])
                    if _cmp_res is not None:
                        _cmp_early_handled = True
                        from backend.agent_runtime.llm_loop import _persist_agent_state_split
                        _persist_agent_state_split(_cmp_ms, agent_id, ctx.session_id, db_agent_id)
                        _cmp_filter_state = _cmp_ms.cmp
                        if _cmp_res.get('decision') not in ('continue', 'init'):
                            _cmp_switched_this_turn = True
                            _logger.info(
                                "CMP boundary(early): %s -> %s (layer %s) for session %s",
                                _cmp_res.get('decision'), _cmp_res.get('target'),
                                _cmp_res.get('layer'), ctx.session_id)
            except Exception:
                _logger.exception("CMP early boundary hook failed — full-history turn")
                _cmp_filter_state = None

        # Build messages for LLM (summary-aware). A prefetch is valid only if
        # it was assembled under the current summary watermark; summarization
        # can advance asynchronously after the background warmup completes.
        summary_record = db.get_summary(ctx.session_id, agent_id=db_agent_id)
        summary_watermark = (summary_record.get('last_message_ts')
                             if summary_record else None)
        _prefetch = None
        if not agent.get('disable_turn_prefetch', 0) and not _cmp_switched_this_turn:
            _prefetch = self._prefetcher.try_get(
                ctx.session_id, summary_watermark=summary_watermark)
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
            system_prompt = _ctx.build_system_prompt(agent, injected_system_vars=agent.get('injected_system_vars'))
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
            return bool(meta.get('busy_ack') or meta.get('busy_rejection')
                        or meta.get('evonet_offline') or meta.get('bash_exec'))

        def _is_slash_command_msg(msg: dict) -> bool:
            """Skip slash command user messages and their assistant responses.

            Slash commands are handled directly by the command executor and must
            never enter LLM context. If they leak into context the LLM may try to
            process them (e.g., re-issuing /restart via its restart tool).
            """
            meta = msg.get('metadata') or {}
            return bool(meta.get('slash_command'))

        # _should_wrap_user_message is defined at module level (also used by handle_message).
        # Keep this alias so the rest of _do_process_inner reads naturally.

        # Will be set to True if describe_image is in the agent's assigned_tool_ids.
        # Must exist before _apply_multimodal so the closure can capture it.
        _has_describe_image = False

        def _apply_multimodal(msg: dict) -> dict:
            """Apply multimodal formatting for user messages with video if agent supports it.

            Images are NEVER auto-fed to the main LLM — images are always accessed via
            the ``describe_image`` tool instead. Audio is likewise never auto-fed —
            agents listen to it via the ``transcribe_audio`` tool.
            """
            if msg.get('role') != 'user':
                return msg
            # Always pop _image_url/_audio_url but NEVER feed them to the LLM.
            msg.pop('_image_url', None)
            msg.pop('_audio_url', None)
            # Append authoritative structured metadata, including the numeric ID
            # required by read_attachment. This also repairs legacy message text
            # that omitted the ID when restored from JSONL.
            _att = msg.pop('attachment_info', None) or msg.get('attachment_info')
            if _att and isinstance(_att, dict):
                _ctx.append_attachment_note(
                    msg,
                    _att,
                    has_describe_image=_has_describe_image,
                    audio_enabled=bool(agent.get('audio_enabled')),
                )
            video = msg.pop('_video_url', None) if agent.get('video_enabled') else msg.pop('_video_url', None) and None
            if not video:
                return msg
            parts = []
            text_content = msg.get('content', '')
            if text_content and text_content not in ('[Image]', '[Audio]', '[Video]'):
                parts.append({"type": "text", "text": text_content})
            if video:
                parts.append({"type": "video_url", "video_url": {"url": video}})
            if not parts or parts[0].get('type') != 'text':
                parts.insert(0, {"type": "text", "text": "What is in this media?"})
            return {**msg, 'content': parts}

        chatlog = chatlog_manager.get(db_agent_id, ctx.session_id)

        # Compute whether describe_image is assigned so the _apply_multimodal
        # closure can conditionally inject the hint.  Done early because
        # _apply_multimodal is called during message building below.
        # Describes whether the agent has (or will receive — via auto-assign
        # below) access to the describe_image tool.
        if _used_prefetch:
            _has_describe_image = 'describe_image' in _agent_ctx_prebuilt.get('assigned_tool_ids', [])
        else:
            _has_describe_image = (
                'describe_image' in db.get_agent_tools(db_agent_id)
                or bool(agent.get('vision_enabled', 1))
            )

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
                    _cur_meta = (_cur_user.get('metadata') or {})
                    _img = _cur_meta.get('image_url')
                    if _img:
                        _cur_msg['_image_url'] = _img
                    _vid = _cur_meta.get('video_url')
                    if _vid:
                        _cur_msg['_video_url'] = _vid
                    _atts = _cur_meta.get('attachment_infos')
                    if isinstance(_atts, list):
                        _cur_msg['attachment_infos'] = _atts
                    _att = _cur_meta.get('attachment_info')
                    if _att:
                        _cur_msg['attachment_info'] = _att
                    messages.append(_apply_multimodal(_cur_msg))
        else:
            # Prefer JSONL-based context if the log has entries for this session.
            # CMP offload: with multiple paths, scope the history window to the
            # active path's segments (shared builder — same as prefetch).
            _jsonl_entries = None
            try:
                from backend.agent_runtime.cmp import assembler as _cmp_asm
                if _cmp_asm.should_filter(_cmp_filter_state):
                    _jsonl_entries = _cmp_asm.build_history(
                        chatlog, summary_record, _cmp_filter_state)
            except Exception:
                _logger.exception("CMP history filter failed — using full history")
                _jsonl_entries = None
            if _jsonl_entries is None:
                _jsonl_entries = chatlog.get_entries_for_llm_trail(
                    after_ts=summary_record.get('last_message_ts') if summary_record else None,
                    **_ctx.trail_history_kwargs(agent_id),
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
                    messages.append(_apply_multimodal(msg))
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
                            messages.append(_ctx.build_message_entry(msg, agent, _has_describe_image))
                else:
                    history = db.get_session_messages(ctx.session_id, limit=50, agent_id=db_agent_id)
                    for msg in history:
                        if not _is_legacy_agent_state_msg(msg) and not _is_ui_only_msg(msg) and not _is_slash_command_msg(msg):
                            messages.append(_ctx.build_message_entry(msg, agent, _has_describe_image))

        # Ensure messages don't end with assistant role (causes prefill error with some APIs)
        while len(messages) > 1 and messages[-1].get('role') == 'assistant':
            messages.pop()

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

        # Inject channel user identity so the agent knows who it's speaking with.
        # This is authoritative for the session and overrides any stale remembered name.
        # Skip if the identity was already injected (e.g. via prefetcher) to avoid
        # piling up duplicates across turns.
        if ctx.channel_id and not ctx.external_user_id.startswith("__agent__"):
            _already_injected = any(
                "## Current User" in (m.get("content") or "")
                for m in messages[:6]
            )
            if not _already_injected:
                user_id_ctx = _ctx.build_user_identity_context(
                    ctx.channel_id, ctx.external_user_id,
                )
                if user_id_ctx:
                    messages.insert(1, {"role": "system", "content": user_id_ctx})

        # Build tool definitions (use prefetched if available)
        if _used_prefetch:
            tools = list(_tools_prebuilt)
            assigned_tool_ids = list(_agent_ctx_prebuilt.get('assigned_tool_ids', []))
            agent_context = dict(_agent_ctx_prebuilt)  # shallow copy to allow mutations
        else:
            tools = _ctx.build_tools(agent)

            # Build agent context for tool backends. Explorers use their own
            # configured tool set; everyone else inherits from the DB.
            if agent.get('is_explorer'):
                from backend.agent_runtime import explorer as _explorer
                assigned_tool_ids = list(_explorer.tool_ids(agent))
            else:
                assigned_tool_ids = db.get_agent_tools(db_agent_id)

            # Inter-agent communication is enabled by the agent-level toggle;
            # send_agent_message is therefore available without a separate tool
            # assignment, matching build_tools() exposure.
            if (agent.get('agent_messaging_enabled') != 0
                    and 'send_agent_message' not in assigned_tool_ids):
                assigned_tool_ids.append('send_agent_message')

            # Inject skill tool IDs from assigned skills into assigned_tool_ids.
            # This mirrors context.py build_tools() Layer 9 auto-injection and
            # ensures the authorization guard allows execution of skill tools
            # that belong to skills explicitly assigned to the agent.
            # Skill tool IDs are namespaced: skill:<skill_id>:<fn_name>
            assigned_skill_ids = set(db.get_agent_skills(db_agent_id))
            if assigned_skill_ids:
                from backend.skills_manager import skills_manager
                _existing = set(assigned_tool_ids)
                for _sd in skills_manager.get_all_skill_tool_defs():
                    _tid = _sd.get('id', '')
                    if not _tid:
                        continue
                    # Parse skill:<skill_id>:<fn_name>
                    _parts = _tid.split(':', 2)
                    if len(_parts) != 3 or _parts[0] != 'skill':
                        continue
                    _skill_id = _parts[1]
                    if _skill_id in assigned_skill_ids and _tid not in _existing:
                        assigned_tool_ids.append(_tid)

            # Auto-assign artifact tools (save_artifact, list_artifacts, fetch_artifact)
            # only when artifacts_enabled is True for the agent.
            if agent.get('artifacts_enabled', True):
                if 'save_artifact' not in assigned_tool_ids:
                    assigned_tool_ids.append('save_artifact')

                # Agents with save_artifact automatically get list_artifacts.
                # fetch_artifact is only auto-assigned for agents with workplace or sandbox;
                # local agents can access artifacts directly via bash/runpy.
                if 'save_artifact' in assigned_tool_ids:
                    if 'list_artifacts' not in assigned_tool_ids:
                        assigned_tool_ids.append('list_artifacts')
                    if agent.get('workplace_id') or agent.get('sandbox_enabled', 0):
                        if 'fetch_artifact' not in assigned_tool_ids:
                            assigned_tool_ids.append('fetch_artifact')

            # Auto-assign send_file to all agents so they can send files via channels.
            # No DB assignment needed — every agent can send file attachments.
            if 'send_file' not in assigned_tool_ids:
                assigned_tool_ids.append('send_file')

            # Agents with vision_enabled automatically get describe_image.
            # No DB assignment needed — every vision-capable agent can analyze images.
            if agent.get('vision_enabled', 1) and 'describe_image' not in assigned_tool_ids:
                assigned_tool_ids.append('describe_image')

            # Agents with audio_enabled automatically get transcribe_audio.
            if agent.get('audio_enabled') and 'transcribe_audio' not in assigned_tool_ids:
                assigned_tool_ids.append('transcribe_audio')

            # Resolve workspace: workplace config takes priority over agent.workspace.
            # For tunnel workplaces, never fall back to the agent's /workspace path —
            # Evonet runs on the remote device and has its own working directory.
            # EXPLORERS: their workspace is set by explorer.build_config to the
            # user-requested absolute path (the 'path' parameter of Explore).
            # Use it directly — the workplace_id is only for SSH backend routing.
            _workplace_id = agent.get('workplace_id') or None
            if agent.get('is_explorer'):
                _workspace = agent.get('workspace') or None
            elif _workplace_id:
                try:
                    import json as _json
                    _workplace = db.get_workplace(_workplace_id)
                    _workplace_cfg = _json.loads(_workplace.get('config', '{}')) if _workplace else {}
                    if _workplace and _workplace.get('type') == 'tunnel':
                        _workspace = _workplace_cfg.get('workspace_path') or None
                    else:
                        _workspace = _workplace_cfg.get('workspace_path') or agent.get('workspace') or None
                except Exception:
                    _workspace = agent.get('workspace') or None
            else:
                _workspace = agent.get('workspace') or None

            # A LID-addressed WhatsApp DM reaches us as bare LID digits, which
            # look exactly like a phone number. Resolve the real identity once
            # here so tools never have to guess from user_id.
            from backend.channels.whatsapp_identity import resolve_identity
            _identity = resolve_identity(ctx.channel_id, ctx.external_user_id)

            agent_context = {
                'id': agent_id,
                '_db_agent_id': agent.get('_db_agent_id', agent_id),
                'name': agent.get('name', ''),
                'agent_name': agent.get('name', ''),
                'agent_model': None,
                'user_id': ctx.external_user_id,
                'user_phone': _identity['user_phone'],
                'user_jid': _identity['user_jid'],
                'user_id_namespace': _identity['user_id_namespace'],
                'channel_id': ctx.channel_id,
                'session_id': ctx.session_id,
                'assigned_tool_ids': assigned_tool_ids,
                'workspace': _workspace,
                'workplace_id': _workplace_id,
                'send_file_allowed_path_regex': agent.get('send_file_allowed_path_regex', ''),
                'is_super': bool(agent.get('is_super')),
                'is_subagent': bool(agent.get('is_subagent')),
                'is_explorer': bool(agent.get('is_explorer')),
                'parent_id': agent.get('parent_id'),
                '_sandbox_parent_session_id': agent.get('_sandbox_parent_session_id'),
                '_sandbox_parent_workspace': agent.get('_sandbox_parent_workspace'),
                'agent_messaging_enabled': bool(agent.get('agent_messaging_enabled')),
                'sandbox_enabled': agent.get('sandbox_enabled', 1),
                'safety_checker_enabled': agent.get('safety_checker_enabled', 1),
                'disable_parallel_tool_execution': agent.get('disable_parallel_tool_execution', 0),
                'disable_turn_prefetch': agent.get('disable_turn_prefetch', 0),
                'variables': db.get_agent_variables_dict(db_agent_id),
                'run_as_user': agent.get('run_as_user'),
                'vision_model_id': agent.get('vision_model_id'),
                'vision_enabled': agent.get('vision_enabled', 1),
                'audio_enabled': agent.get('audio_enabled', 0),
                'messaging_acl': agent.get('messaging_acl'),
                'messaging_acl_mode': agent.get('messaging_acl_mode', 'whitelist'),
                'enable_atg': bool(agent.get('enable_atg')) and bool(agent.get('enable_agent_state')),
                'enable_cmp': bool(agent.get('enable_cmp')) and bool(agent.get('enable_agent_state')),
            }
        _last_user = chatlog.get_last_entry(types=frozenset({'user'}))
        assigned_tool_ids, tools = _apply_restart_origin_guard(
            agent_context, assigned_tool_ids, tools,
            (_last_user or {}).get('metadata') or {},
        )

        # Propagate trusted identity metadata from the channel into agent_context
        # so synchronous lifecycle gates (e.g. workflow_guard) receive stable,
        # attested message/attachment identifiers.
        _trusted_meta = (_last_user or {}).get('metadata') or {}
        if isinstance(_trusted_meta, dict):
            if _trusted_meta.get('channel_message_id'):
                agent_context['trusted_message_id'] = str(_trusted_meta['channel_message_id'])
            if _trusted_meta.get('attachment_info'):
                _a_info = _trusted_meta['attachment_info']
                _att_infos = _a_info if isinstance(_a_info, list) else [_a_info]
                _att_ids = []
                _att_mime_types = []
                _att_paths = []
                for _att in _att_infos:
                    if not isinstance(_att, dict):
                        continue
                    _att_id = _att.get('id') or _att.get('attachment_id')
                    if _att_id is not None:
                        _att_ids.append(str(_att_id))
                    if _att.get('mime_type'):
                        _att_mime_types.append(str(_att['mime_type']))
                    _att_path = _att.get('workplace_path') or _att.get('file_path')
                    if _att_path:
                        _att_paths.append(str(_att_path))
                if _att_ids:
                    agent_context['trusted_attachment_ids'] = _att_ids
                if _att_mime_types:
                    agent_context['trusted_attachment_mime_types'] = _att_mime_types
                if _att_paths:
                    agent_context['trusted_attachment_paths'] = _att_paths

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
                    if _meta.get('injected_system_vars') is not None:
                        agent_context['injected_system_vars'] = _meta['injected_system_vars']
                    for _key in ('report_to_id', 'report_to_channel_id', 'session_id', 'reply_to_id'):
                        if _meta.get(_key) is not None:
                            agent_context[f'origin_{_key}'] = _meta[_key]
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
                            if _meta.get('injected_system_vars') is not None:
                                agent_context['injected_system_vars'] = _meta['injected_system_vars']
                            for _key in ('report_to_id', 'report_to_channel_id', 'session_id', 'reply_to_id'):
                                if _meta.get(_key) is not None:
                                    agent_context[f'origin_{_key}'] = _meta[_key]
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
                    ms = AgentState(mode="execute", auto_trivial=True, always_execute=bool(agent.get('always_execute')))
                else:
                    ms = AgentState(always_execute=bool(agent.get('always_execute')))
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
                        ms = AgentState(always_execute=bool(agent.get('always_execute')))
            # Cross-task boundary handling. CMP (when enabled) owns it: the
            # detector routes the turn (continue/return/branch) and a branch
            # IS the ATG re-arm — a fresh plan cycle on its own path. Agents
            # with ATG but not CMP keep the original 2-way re-arm.
            _boundary_text = ""
            if not agent.get('is_subagent'):
                for _msg in reversed(messages):
                    if _msg.get('role') == 'user':
                        _c = _msg.get('content', '')
                        _boundary_text = (
                            next((p.get('text', '') for p in _c
                                  if isinstance(p, dict) and p.get('type') == 'text'), '')
                            if isinstance(_c, list) else _c)
                        break
            # The early hook (before context assembly) normally owns CMP; this
            # late slot only covers turns it skipped — e.g. brand-new sessions
            # where agent state didn't exist yet at assembly time (first-path
            # init happens here, after the classifier created ms).
            _cmp_handled = _cmp_early_handled
            if (not _cmp_early_handled
                    and agent.get('enable_cmp') and agent.get('enable_agent_state')
                    and not agent.get('is_subagent')):
                try:
                    from backend.agent_runtime import cmp as _cmp_pkg
                    _cmp_chatlog = chatlog_manager.get(db_agent_id, ctx.session_id)
                    _cmp_res = _cmp_pkg.on_turn_boundary(
                        agent, ms, _cmp_chatlog, _boundary_text,
                        session_id=ctx.session_id, agent_id=agent['id'])
                    _cmp_handled = _cmp_res is not None
                    if _cmp_res and _cmp_res.get('decision') not in ('continue', 'init'):
                        _logger.info("CMP boundary: %s -> %s (layer %s) for session %s",
                                     _cmp_res.get('decision'), _cmp_res.get('target'),
                                     _cmp_res.get('layer'), ctx.session_id)
                except Exception:
                    _logger.exception("CMP boundary check failed — keeping current state")

            # ATG re-arm (atg-only agents): when the previous task graph is
            # finished and this message is a brand-new complex task, re-enter
            # the plan/compile cycle so the new task gets its own graph.
            if (not _cmp_handled and not is_new_session and ms.mode == 'execute'
                    and agent.get('enable_atg') and not agent.get('is_subagent')):
                try:
                    from backend.agent_runtime import atg as _atg_pkg
                    if _atg_pkg.maybe_rearm_atg(agent, ms, _boundary_text):
                        _logger.info("ATG re-armed for session %s — new complex task detected",
                                     ctx.session_id)
                except Exception:
                    _logger.exception("ATG re-arm check failed — keeping current state")

            # Sub-agents receive delegated tasks from their parent and
            # should never require plan/approval cycles. Force execute mode.
            if agent.get('is_subagent'):
                ms = AgentState(mode="execute", auto_trivial=True)
            agent_context['agent_state'] = ms

        # --- Staleness detection ---
        # Detect when the previous user message is older than the threshold
        # and mark as stale so the wrapper prefix can inject context awareness.
        is_stale = False
        stale_threshold = STALE_SESSION_THRESHOLD_SECONDS
        _stale_skip = (
            ctx.external_user_id.startswith('__agent__') or
            ctx.external_user_id.startswith('__system__')
        )
        if not _stale_skip:
            _stale_enabled = agent.get('stale_session_injection_enabled')
            if _stale_enabled is None:
                _stale_enabled = STALE_SESSION_INJECTION_ENABLED
            if _stale_enabled:
                _recent = chatlog.tail(limit=50)
                _user_entries = [e for e in _recent if e.get('type') == 'user']
                if len(_user_entries) >= 2:
                    _prev_ts = _user_entries[-2].get('ts', 0)
                    _gap = time.time() - _prev_ts
                    if _gap > stale_threshold:
                        is_stale = True

            # --- Long-gap absence injection ---
            # Inject system context when user hasn't messaged in >= LONG_GAP_WEEKS.
            # Uses standalone get_last_entry() (~0.1ms per turn) for simplicity.
            _long_gap_ctx = _build_long_gap_context(chatlog, LONG_GAP_WEEKS)
            if _long_gap_ctx:
                # Dedup: avoid piling up multiple copies across turns
                _already_injected = any(
                    "## Long Absence Notice" in (m.get("content") or "")
                    for m in messages[:10]
                )
                if not _already_injected:
                    messages.insert(1, {"role": "system", "content": _long_gap_ctx})

        # Apply preference wrapper prefix to user messages if enabled
        _apply_wrapper_prefix(messages, _should_wrap_user_message(agent),
                              is_stale=is_stale, stale_threshold=stale_threshold,
                              loaded_skills_count=len(self._session_skill_tools.get(ctx.session_id, {})))

        # Sub-agents run delegated tasks with plan/approval disabled (see force-execute
        # above). Tell the LLM explicitly so it doesn't emit a plan and stop.
        if agent.get('is_subagent'):
            _already_subagent_directive = any(
                "running as a sub-agent on a delegated task" in (m.get("content") or "")
                for m in messages[:5]
            )
            if not _already_subagent_directive:
                messages.insert(1, {"role": "system", "content": SUBAGENT_EXECUTE_DIRECTIVE})

        # --- Background jobs injection ---
        # The agent sees a background process once, in the bash result that
        # spawned it; nothing surfaces it again. Re-state what is still running
        # so it does not leave processes to go stale. Appended at the END: the
        # list changes every turn, and inserting it up front would invalidate the
        # prompt prefix cache for the whole history. Must run after
        # _apply_wrapper_prefix, which identifies the current user message by
        # position (last in the list).
        if BACKGROUND_JOBS_INJECTION_ENABLED:
            try:
                from backend.agent_runtime.background_jobs import build_context_block
                _bg_ctx = build_context_block(
                    ctx.session_id, agent.get('agent_id') or agent.get('id') or '')
                if _bg_ctx:
                    messages.append({"role": "system", "content": _bg_ctx})
            except Exception:
                _logger.exception("[bgjob] context injection failed — continuing")

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
            from backend.llm_usage_events import usage_context
            if agent.get('is_kb_organizer'):
                _usage_source = 'kb_organizer'   # distinct usage category for the token monitor
            elif agent.get('is_explorer'):
                _usage_source = 'explorer'
            else:
                _usage_source = 'agent_turn'
            with usage_context(_usage_source, agent_id, agent.get('name'), ctx.session_id):
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

    def get_compiled_context(self, agent_id: str, user_id: str = None) -> dict:
        """Return the compiled system prompt and tool definitions for an agent."""
        return _ctx.get_compiled_context(agent_id, user_id=user_id)

    def _build_message_entry(self, msg: dict, agent: dict) -> dict:
        """Convert a DB message row into an LLM message dict.

        Safety net: applies RTK compression before falling back to blunt
        truncation, mirroring the logic in context.py._build_message_entry().
        """
        entry = {"role": msg["role"]}
        _msg_meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
        msg_video = _msg_meta.get("video_url") if _msg_meta else None
        # Images are NEVER auto-fed — use describe_image tool instead.
        # Audio is likewise never auto-fed — use transcribe_audio tool instead.
        has_video = msg_video and agent.get("video_enabled")
        if has_video:
            parts = []
            text_content = msg.get("content", "")
            if text_content and text_content not in ("[Image]", "[Audio]", "[Video]"):
                parts.append({"type": "text", "text": text_content})
            parts.append({"type": "video_url", "video_url": {"url": msg_video}})
            if not parts or parts[0].get("type") != "text":
                parts.insert(0, {"type": "text", "text": "What is in this media?"})
            entry["content"] = parts
        elif msg.get("content"):
            content = msg["content"]
            # Safety net: try RTK compression before falling back to blunt truncation
            if msg.get("role") == "tool" and len(content) > MAX_TOOL_RESULT_CHARS:
                try:
                    from backend.token_compressor.compressor_registry import get_registry
                    reg = get_registry()
                    hint = _ctx.command_hint_from_content(content)
                    compressed = reg.compress(hint, 0, content)
                    if compressed != content:
                        content = compressed
                except Exception:
                    pass

                # Still apply blunt truncation if RTK didn't shrink enough
                if len(content) > MAX_TOOL_RESULT_CHARS:
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
        # Record the deferral so the pending user message is auto-resumed once the
        # agent is free and unfocused (drained in _send_free_notification). Queued
        # on the opt-in branch too — the real answer supersedes the notification.
        self._queue_deferred_resume(agent_id, session_id, external_user_id, channel_id)
        # Send via channel if applicable
        if channel_id:
            try:
                instance = channel_manager._active.get(channel_id)
                if instance and instance.is_running:
                    instance.send_message(external_user_id, reply)
                    if isinstance(instance, BaseChannel) and instance.has_send_error(external_user_id):
                        err = instance.get_send_error(external_user_id)
                        _logger.warning(
                            "Channel send error for busy rejection (session %s): %s",
                            session_id, err,
                        )
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

    # Sessions rejected while the agent was focus-busy, awaiting auto-resume:
    # agent_id -> {session_id: {'external_user_id': str, 'channel_id': str|None}}
    # Drained by _send_free_notification once the agent is free and unfocused.
    _deferred_resume_pending: dict = {}
    _deferred_resume_lock = threading.Lock()

    @classmethod
    def _queue_deferred_resume(cls, agent_id: str, session_id: str,
                                external_user_id: str, channel_id: Optional[str]) -> None:
        """Record a busy-rejected session so it is re-run when the agent frees up."""
        with cls._deferred_resume_lock:
            cls._deferred_resume_pending.setdefault(agent_id, {})[session_id] = {
                'external_user_id': external_user_id,
                'channel_id': channel_id,
            }

    def clear_session(self, agent_id: str, external_user_id: str, channel_id: Optional[str] = None) -> None:
        """Clear chat history for a user's session."""
        session_id = db.get_or_create_session(agent_id, external_user_id, channel_id)
        db.clear_session(session_id, agent_id=agent_id)
        self._session_skill_mds.pop(session_id, None)
        self._session_skill_tools.pop(session_id, None)

    def remove_session_skill(self, session_id: str, skill_id: str) -> bool:
        """Remove a skill from a session (both tools and MD context). Thread-safe."""
        removed = False
        if session_id in self._session_skill_tools and skill_id in self._session_skill_tools[session_id]:
            del self._session_skill_tools[session_id][skill_id]
            removed = True
        if session_id in self._session_skill_mds and skill_id in self._session_skill_mds[session_id]:
            del self._session_skill_mds[session_id][skill_id]
            removed = True
        return removed

    def get_session_skills(self, session_id: str) -> list[dict]:
        """Return loaded skills for a session. Thread-safe: copies the dict before iterating."""
        skills_data = dict(self._session_skill_tools.get(session_id, {}))
        return [{"skill_id": sk_id, "tool_count": len(tool_defs)}
                for sk_id, tool_defs in skills_data.items()]

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

    def send_file_as_bot(self, session_id: str, file_path: str,
                         caption: str | None = None,
                         mime_type: str | None = None) -> bool:
        """Send a file via channel and record it as a chat entry + attachment."""
        session = db.get_session_with_details(session_id)
        if not session:
            return False
        agent_id = session['agent_id']
        external_user_id = session['external_user_id']
        channel_id = session.get('channel_id')
        channel_type = session.get('channel_type')

        # Send via channel if available
        channel_ok = True
        if channel_id:
            instance = channel_manager._active.get(channel_id)
            if instance and instance.is_running:
                try:
                    channel_ok = instance.send_file(external_user_id, file_path, caption, mime_type)
                except Exception as e:
                    _logger.error("send_file_as_bot channel error: %s", e)
                    channel_ok = False

        if not channel_ok:
            return False

        filename = os.path.basename(file_path)
        guessed_mime = mime_type or mimetypes.guess_type(file_path)[0] or 'application/octet-stream'
        size_bytes = os.path.getsize(file_path)
        is_image = guessed_mime.startswith('image/') if guessed_mime else False

        # 1. Save attachment record
        attachment_id = db.save_attachment(
            agent_id=agent_id,
            session_id=session_id,
            filename=filename,
            file_path=file_path,
            external_user_id=external_user_id,
            channel_id=channel_id,
            channel_type=channel_type or '',
            original_filename=filename,
            mime_type=guessed_mime,
            file_type='document',
            size_bytes=size_bytes,
        )

        # 2. Build attachment_info metadata for the chat entry
        attachment_info = {
            'attachment_id': attachment_id,
            'filename': filename,
            'mime_type': guessed_mime,
            'size_bytes': size_bytes,
            'is_image': is_image,
            'file_path': file_path,
        }

        # 3. Write chat entry
        if is_image:
            content = f"![{filename}](/api/attachments/{attachment_id}/view)"
            if caption:
                content = f"{caption}\n" + content
        elif caption:
            content = f"{caption}\n[File: {filename}]"
        else:
            content = f"[File: {filename}]"

        chatlog = chatlog_manager.get(agent_id, session_id)
        chatlog.append({
            'type': 'final',
            'session_id': session_id,
            'content': content,
            'metadata': {'attachment_info': attachment_info, 'channel': channel_type},
        })

        # Also persist in main chat messages table
        db.add_chat_message(session_id, 'assistant', content,
                            agent_id=agent_id,
                            metadata={'attachment_info': attachment_info})

        return True

    def _resolve_media_url(self, url: str) -> Optional[str]:
        """Resolve an internal media URL to a local file path, or None.

        Supports artifact URLs (/api/agents/<aid>/artifacts/<filename>) and
        attachment URLs (/api/attachments/<id>/view). Only returns paths that
        exist on disk and stay inside the artifact directory (no traversal).
        """
        if not url:
            return None
        url = url.split('?')[0].split('#')[0]

        m = _ARTIFACT_URL_RE.match(url)
        if m:
            aid, filename = m.group(1), m.group(2)
            if '..' in filename or '/' in filename:
                return None
            safe_root = os.path.realpath(
                os.path.join(_BASE_DIR, 'shared', 'agents', aid, 'artifacts'))
            candidate = os.path.realpath(os.path.join(safe_root, filename))
            if candidate.startswith(safe_root + os.sep) and os.path.isfile(candidate):
                return candidate
            return None

        m = _ATTACHMENT_URL_RE.match(url)
        if m:
            try:
                row = db.get_attachment(int(m.group(1)))
            except Exception:
                return None
            if row and row.get('file_path') and os.path.isfile(row['file_path']):
                return row['file_path']
        return None

    def _strip_media_embeds(self, text: str, session_id: str) -> str:
        """Safety net for external channels: convert HTML/Markdown image embeds
        in a final reply into real file attachments.

        Resolvable artifact/attachment URLs are delivered via send_file_as_bot()
        and the embed markup is removed from the outgoing text so the chat client
        shows a clean message plus the attachment. Unresolvable embeds are also
        stripped (they would otherwise arrive as raw text).
        """
        if not text:
            return text

        def _handle(url: str) -> str:
            path = self._resolve_media_url(url)
            if path:
                try:
                    self.send_file_as_bot(session_id, path)
                except Exception as e:
                    _logger.warning("send_file_as_bot failed for embed %s: %s", url, e)
            else:
                _logger.info(
                    "Stripped unresolvable media embed from outgoing channel text: %s", url)
            return ''

        text = _IMG_EMBED_RE.sub(lambda m: _handle(m.group(1)), text)
        text = _MD_IMG_EMBED_RE.sub(lambda m: _handle(m.group(1)), text)
        return text

    def send_as_user(self, session_id: str, text: str,
                     image_url: str | None = None,
                     audio_url: str | None = None,
                     video_url: str | None = None,
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
                # Check for any attachments created by the handler (e.g. /dump)
                attachment_info = None
                try:
                    attachments = db.list_session_attachments(session_id, agent_id)
                    if attachments:
                        att = attachments[0]
                        guessed_mime = att.get('mime_type') or 'application/octet-stream'
                        att_info = {
                            'attachment_id': att['id'],
                            'filename': att.get('filename', ''),
                            'mime_type': guessed_mime,
                            'size_bytes': att.get('size_bytes', 0),
                            'is_image': guessed_mime.startswith('image/'),
                            'file_path': att.get('file_path', ''),
                        }
                        attachment_info = att_info
                except Exception:
                    pass
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
                    'attachment_info': attachment_info,
                })
                # Signal the client to clear the chat UI when the clear command was used
                if cmd_name == 'clear':
                    event_stream.emit('session_clear', {
                        'session_id': session_id,
                        'agent_id': agent_id,
                    })
                self._prefetcher.invalidate(session_id)
                return response  # return response text so caller can include it in API response
            # Unknown command — fall through to normal LLM processing

        meta = {'user_perspective': True}
        if image_url:
            meta['image_url'] = image_url
        if audio_url:
            meta['audio_url'] = audio_url
        if video_url:
            meta['video_url'] = video_url
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
            'audio_url': audio_url,
            'video_url': video_url,
        })

        # Mid-loop injection: if session is currently processing, inject message
        # into the active loop instead of queuing a new task.
        if agent and self._is_busy(session_id):
            self._get_inject_queue(session_id).put({
                'role': 'user',
                'content': text,
            })
            event_stream.emit('message_injected', {
                'agent_id': agent_id,
                'agent_name': agent.get('name', ''),
                'session_id': session_id,
                'external_user_id': external_user_id,
                'channel_id': channel_id,
                'message': text,
            })
            return True

        # Enqueue for agent processing (fire-and-forget)
        if agent and agent.get('enabled', True):
            task = _QueueTask(agent, SessionContext(session_id, external_user_id, channel_id),
                              send_via_channel=False)
            self._message_queue.put(task)

        return True

    def resume_session(self, agent: dict, session_id: str,
                       external_user_id: str, channel_id: str | None = None,
                       send_via_channel: bool = False) -> None:
        """Re-enqueue a session for agent processing without saving a new message.

        Used at startup to follow up on unreplied user messages that were left
        pending after a server restart, and by the deferred-resume drain to
        answer messages rejected while the agent was focus-busy (pass
        send_via_channel=True there so channel users receive the answer).
        """
        if not agent.get('enabled', True):
            return
        task = _QueueTask(agent, SessionContext(session_id, external_user_id, channel_id),
                          send_via_channel=send_via_channel)
        self._message_queue.put(task)
