"""Per-chat outbound dispatcher for WhatsApp with adaptive pacing and pooling.

Reduces suspension risk by serialising delivery per recipient, pooling
text that arrives close together, applying bounded adaptive delays with
jitter, and maintaining composing/paused presence throughout.

All waiting happens in dedicated worker threads — the runtime and
callback threads are never blocked by sleeps.
"""

import hashlib
import logging
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default global settings (overridden by app_settings DB table)
# ---------------------------------------------------------------------------
_DEFAULT_SETTINGS = {
    "whatsapp_safe_delivery_enabled": "1",
    "whatsapp_pool_window_seconds": "2.0",
    "whatsapp_min_send_interval_seconds": "2.0",
    "whatsapp_typing_chars_per_second": "20.0",
    "whatsapp_max_typing_delay_seconds": "15.0",
    "whatsapp_delay_jitter_ratio": "0.15",
    "whatsapp_max_outbound_per_minute": "30",
    "whatsapp_natural_formatting_enabled": "1",
}


class WhatsAppOutboundDispatcher:
    """Per-chat FIFO outbound queue with adaptive pacing for one WhatsAppChannel.

    Public API
    ----------
    enqueue(external_user_id, text, *, session_id=None, is_final=True)
        Queue a message for delivery.  *Never* blocks the calling thread.

    shutdown()
        Stop accepting new work, cancel pending timers/workers, and send
        ``paused`` presence for any active chat.  Safe to call multiple
        times.

    Design
    ------
    - One ``deque`` per canonical recipient (``external_user_id``).
    - One processing worker thread per active chat; created on demand and
      terminates when the queue is drained.
    - Text arriving within the *pool window* of the last queue item is
      merged with paragraph boundaries.
    - A **final** message (``is_final=True``) absorbs any still-pending
      intermediate items.
    - Adaptive delay = ``len(text) / typing_chars_per_second``, bounded by
      ``max_typing_delay_seconds``, plus a small cryptographically-seeded
      jitter.
    - Hard ``min_send_interval_seconds`` between successive sends to the
      same chat.
    - Inter-chunk pacing for messages split at the 4096-byte boundary.
    - ``composing`` presence shown during the wait; ``paused`` always sent
      on completion / cancellation / error / shutdown.
    """

    def __init__(self, channel, settings_getter: Callable[[str, str], str]):
        """Initialise the dispatcher.

        Parameters
        ----------
        channel:
            A ``WhatsAppChannel`` instance.  Must provide at least
            ``_do_send()``, ``send_typing()``, and ``_split_message()``.
        settings_getter:
            ``Callable[[key, default], value]`` — typically a closure over
            ``db.get_setting()`` so that global overrides are live-read
            without restart.
        """
        self._channel = channel
        self._settings = settings_getter

        # Per-recipient state
        self._queues: Dict[str, deque] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()  # protects _queues / _locks
        self._workers: Dict[str, threading.Event] = {}  # cancel token per chat
        self._last_send: Dict[str, float] = {}
        self._pool_deadline: Dict[str, float] = {}

        # Per-channel rate limiter
        self._outbound_window: deque = deque()  # (timestamp,) per send
        self._rate_limit_lock = threading.Lock()

        self._running = True
        self._shutdown_lock = threading.Lock()

        # A reach-out restriction applies to the entire WhatsApp account.
        self._restriction_lock = threading.Lock()
        self._restriction_until = 0.0
        self._restriction_reason: Optional[str] = None

        # Dedup short-term store: (external_user_id, text_digest) -> last_seen_ts
        self._dedup: Dict[tuple, float] = {}
        self._dedup_lock = threading.Lock()
        self._dedup_ttl = 30.0  # seconds

    def pause_for_restriction(self, ends_at: Optional[str] = None,
                              reason: Optional[str] = None) -> None:
        """Pause delivery until the bridge deadline or an explicit resume."""
        until = float("inf")
        if ends_at:
            try:
                parsed = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                until = time.monotonic() + max(0.0, parsed.timestamp() - time.time())
            except (TypeError, ValueError):
                _logger.warning("Invalid WhatsApp restriction deadline: %s", ends_at)
        with self._restriction_lock:
            self._restriction_until = until
            self._restriction_reason = reason
        self._emit("restriction_paused", restriction_ends=ends_at,
                   restriction_reason=reason)

    def resume_after_restriction(self, source: str = "operator") -> None:
        """Clear a bridge restriction after expiry or an operator action."""
        with self._restriction_lock:
            was_paused = self._restriction_until > 0
            self._restriction_until = 0.0
            self._restriction_reason = None
        if was_paused:
            self._emit("restriction_resumed", resume_source=source)

    def _restriction_active(self) -> bool:
        with self._restriction_lock:
            if self._restriction_until == 0:
                return False
            if time.monotonic() < self._restriction_until:
                return True
        self.resume_after_restriction(source="deadline")
        return False

    def _emit(self, event: str, external_user_id: Optional[str] = None,
              session_id: Optional[str] = None, **metadata) -> None:
        """Emit queue telemetry using identifiers and metadata, never text."""
        payload = {
            "event": event,
            "channel_type": "whatsapp",
            "channel_id": getattr(self._channel, "channel_id", None),
            "agent_id": getattr(self._channel, "agent_id", None),
            "external_user_id": external_user_id,
            "session_id": session_id,
            **metadata,
        }
        try:
            from backend.event_stream import event_stream
            event_stream.emit("whatsapp_outbound_queue", payload)
        except Exception:
            _logger.debug("Unable to emit WhatsApp queue event %s", event,
                          exc_info=True)

    def _queue_depth(self, external_user_id: str) -> int:
        with self._global_lock:
            queue = self._queues.get(external_user_id)
            return len(queue) if queue else 0

    # ------------------------------------------------------------------
    # Settings helpers
    # ------------------------------------------------------------------

    def _setting(self, key: str, default: str) -> str:
        """Read a global setting, falling back to the class default."""
        return self._settings(key, _DEFAULT_SETTINGS.get(key, default))

    def _setting_float(self, key: str, default: str) -> float:
        try:
            return float(self._setting(key, default))
        except (ValueError, TypeError):
            return float(default)

    def _setting_bool(self, key: str, default: str) -> bool:
        return self._setting(key, default) == "1"

    def _setting_int(self, key: str, default: str) -> int:
        try:
            return int(self._setting(key, default))
        except (ValueError, TypeError):
            return int(default)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(self, external_user_id: str, text: str, *,
                session_id: Optional[str] = None,
                is_final: bool = True):
        """Queue *text* for delivery to *external_user_id*.

        Parameters
        ----------
        is_final:
            When ``True``, any still-pending intermediate items for the
            same recipient are absorbed into this message and a new
            pooling window is started.  Intermediate items (``False``)
            are pooled together and only sent after their pooling window
            expires *unless* a final item arrives first.
        """
        if not self._running:
            _logger.warning(
                "dispatcher shutdown — dropping outbound to %s (channel %s)",
                external_user_id, getattr(self._channel, "channel_id", "?"))
            return

        text = (text or "").strip()
        if not text or text == "(No response)":
            return

        # Deduplicate identical text within a short window
        dedup_key = (external_user_id, hash(text))
        now = time.monotonic()
        last = self._dedup.get(dedup_key)
        if last is not None and (now - last) < self._dedup_ttl:
            _logger.debug("dedup suppressed duplicate to %s", external_user_id)
            self._emit("deduplicated", external_user_id, session_id,
                       queue_depth=self._queue_depth(external_user_id))
            return
        self._dedup[dedup_key] = now
        self._prune_dedup(now)

        with self._global_lock:
            lock = self._locks.get(external_user_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[external_user_id] = lock
                self._queues[external_user_id] = deque()

        with lock:
            queue: deque = self._queues[external_user_id]
            pool_window = self._setting_float(
                "whatsapp_pool_window_seconds", "2.0")
            pool_deadline = self._pool_deadline.get(external_user_id, 0)

            if queue and now < pool_deadline:
                # Pool: merge with the *last* item in the queue
                last_item = queue[-1]
                last_item["text"] += "\n\n" + text
                if is_final:
                    last_item["is_final"] = True
                _logger.debug(
                    "pooled %s item for %s (queue depth %d)",
                    "final" if is_final else "intermediate",
                    external_user_id, len(queue))
                self._emit("pooled", external_user_id, session_id,
                           queue_depth=len(queue))
            else:
                item = {
                    "text": text,
                    "session_id": session_id,
                    "is_final": is_final,
                    "queued_at": now,
                }
                queue.append(item)
                self._emit("queued", external_user_id, session_id,
                           queue_depth=len(queue))

            # If this is a final message, absorb pending intermediates
            # that are ahead of us in the queue (they arrived before but
            # their pool window hasn't expired).  We take their text and
            # drop them.
            if is_final and len(queue) >= 2:
                merged_text = ""
                keep = []
                while queue:
                    qi = queue.popleft()
                    if not qi["is_final"]:
                        merged_text += ("\n\n" if merged_text else "") + qi["text"]
                    else:
                        keep.append(qi)
                        break
                # Remaining items stay in queue. The final item may have
                # pooled into the preceding entry, so prepend to its actual
                # retained queue item rather than a local variable.
                remain = list(queue)
                queue.clear()
                for ki in keep:
                    queue.append(ki)
                for ri in remain:
                    queue.append(ri)
                if merged_text and queue:
                    queue[0]["text"] = merged_text + "\n\n" + queue[0]["text"]

            # Reset pool deadline
            self._pool_deadline[external_user_id] = now + pool_window

            # Start worker if none is active
            cancel = self._workers.get(external_user_id)
            if cancel is None:
                cancel = threading.Event()
                self._workers[external_user_id] = cancel
                worker = threading.Thread(
                    target=self._worker,
                    args=(external_user_id, cancel),
                    daemon=True,
                    name=f"wa-dispatch-{external_user_id}",
                )
                worker.start()

    def shutdown(self):
        """Stop dispatcher — cancel workers and send paused presence."""
        with self._shutdown_lock:
            if not self._running:
                return
            self._running = False

        _logger.info(
            "WhatsApp dispatcher shutdown — canceling %d active workers "
            "for channel %s",
            len(self._workers),
            getattr(self._channel, "channel_id", "?"),
        )
        with self._global_lock:
            for uid, cancel in list(self._workers.items()):
                cancel.set()
                try:
                    self._channel.send_typing(uid, state="paused")
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Internal: worker thread
    # ------------------------------------------------------------------

    def _worker(self, external_user_id: str, cancel: threading.Event):
        """Process queue items for one chat sequentially."""
        lock = self._locks.get(external_user_id)
        if lock is None:
            return  # shouldn't happen

        pool_window = self._setting_float("whatsapp_pool_window_seconds", "2.0")

        while self._running and not cancel.is_set():
            # Wait for an item that is ready to send
            item = None
            if self._restriction_active():
                self._emit("throttled", external_user_id,
                           queue_depth=self._queue_depth(external_user_id),
                           throttle_reason="reachout_restriction")
                cancel.wait(1.0)
                continue
            while self._running and not cancel.is_set():
                with lock:
                    queue = self._queues.get(external_user_id)
                    if not queue or not queue:
                        # Queue empty — worker done
                        with self._global_lock:
                            self._workers.pop(external_user_id, None)
                        return

                    peek = queue[0]
                    peek_age = time.monotonic() - peek["queued_at"]

                    if peek["is_final"] or peek_age >= pool_window:
                        # Ready to send: pop all ready items and merge
                        merged = self._pop_ready_impl(
                            lock, external_user_id, pool_window)
                        if merged is None:
                            continue  # retry
                        item = merged
                        break
                    else:
                        # Not ready yet — wait for pool window
                        pass

                if item is None:
                    # Sleep a short tick so we can recheck
                    cancel.wait(0.1)
                    continue

            if item is None or cancel.is_set():
                break

            # --- Deliver ---
            try:
                retry = self._deliver(item, external_user_id)
                if retry:
                    with lock:
                        queue = self._queues.get(external_user_id)
                        if queue is not None:
                            queue.appendleft(item)
                            self._emit(
                                "queued", external_user_id,
                                item.get("session_id"),
                                queue_depth=len(queue),
                                queue_reason="throttle_pause",
                            )
                    cancel.wait(0.5)
            except Exception:
                _logger.exception(
                    "dispatcher delivery failed for %s", external_user_id)

        # Worker exiting — clean up
        with self._global_lock:
            self._workers.pop(external_user_id, None)

    def _pop_ready_impl(self, _lock: threading.Lock,
                        external_user_id: str,
                        pool_window: float) -> Optional[dict]:
        """Pop all ready items from the queue for *external_user_id*."""
        queue = self._queues.get(external_user_id)
        if not queue:
            return None

        now = time.monotonic()
        merged_text = ""
        merged_session: Optional[str] = None
        popped = 0

        while queue:
            peek = queue[0]
            peek_age = now - peek["queued_at"]
            if not peek["is_final"] and peek_age < pool_window:
                break  # not ready yet
            qi = queue.popleft()
            popped += 1
            merged_text += ("\n\n" if merged_text else "") + qi["text"]
            if qi.get("session_id"):
                merged_session = qi["session_id"]

        if popped == 0:
            return None

        return {
            "text": merged_text,
            "session_id": merged_session,
            "sent_at": now,
        }

    # ------------------------------------------------------------------
    # Internal: delivery
    # ------------------------------------------------------------------

    def _deliver(self, item: dict, external_user_id: str):
        """Send one queued item with adaptive delay and typing presence."""
        enabled = self._setting_bool("whatsapp_safe_delivery_enabled", "1")
        text = item["text"]
        session_id = item.get("session_id")

        # Format text
        if self._setting_bool("whatsapp_natural_formatting_enabled", "1"):
            text = _natural_whatsapp_format(text)

        # Split into chunks (4096-char WhatsApp limit)
        chunks = _split_message(text)

        if not enabled:
            # Passthrough — no pacing
            for chunk in chunks:
                self._channel._do_send(
                    external_user_id, chunk, session_id=session_id)
            self._channel.send_typing(external_user_id, state="paused")
            self._last_send[external_user_id] = time.monotonic()
            return

        # --- Adaptive delay ---
        typing_cps = self._setting_float(
            "whatsapp_typing_chars_per_second", "20.0")
        max_delay = self._setting_float(
            "whatsapp_max_typing_delay_seconds", "15.0")
        jitter_ratio = self._setting_float("whatsapp_delay_jitter_ratio", "0.15")
        min_interval = self._setting_float(
            "whatsapp_min_send_interval_seconds", "2.0")

        # Base delay proportional to text length
        base_delay = min(len(text) / typing_cps, max_delay)

        # Additional delay per chunk (sending many chunks feels like more typing)
        chunk_bonus = (len(chunks) - 1) * 1.0

        # Hard minimum interval since last send to this chat
        now = time.monotonic()
        elapsed = now - self._last_send.get(external_user_id, 0)
        min_dist = max(0.0, min_interval - elapsed)

        delay = max(min_dist, base_delay + chunk_bonus)

        # Jitter: ± jitter_ratio * delay, using crypto-seeded randomness
        jitter = (secrets.randbelow(1000) / 1000.0 - 0.5) * 2 * delay * jitter_ratio
        delay = max(0.1, delay + jitter)

        # Cap at max_delay
        delay = min(delay, max_delay + (len(chunks) - 1) * 1.0)

        _logger.debug(
            "dispatcher delay=%0.2fs (text_len=%d chunks=%d min_dist=%0.2f) for %s",
            delay, len(text), len(chunks), min_dist, external_user_id)
        self._emit("delayed", external_user_id, session_id,
                   delay_seconds=round(delay, 3), queue_depth=self._queue_depth(external_user_id))

        # Rate-limit check (circuit breaker)
        if not self._reserve_outbound_slot():
            _logger.warning(
                "dispatcher rate limit exceeded — throttling send to %s",
                external_user_id)
            self._emit("throttled", external_user_id, session_id,
                       queue_depth=self._queue_depth(external_user_id),
                       throttle_reason="outbound_quota")
            return True

        # --- Typing indicator + wait ---
        try:
            self._channel.send_typing(external_user_id, state="composing")
        except Exception:
            _logger.debug("composing signal failed for %s", external_user_id)

        # Wait in worker thread (NEVER blocks runtime)
        self._interruptible_sleep(delay)

        # Ensure we're still running
        if not self._running:
            try:
                self._channel.send_typing(external_user_id, state="paused")
            except Exception:
                pass
            return

        if self._restriction_active():
            self._emit("throttled", external_user_id, session_id,
                       queue_depth=self._queue_depth(external_user_id),
                       throttle_reason="reachout_restriction")
            try:
                self._channel.send_typing(external_user_id, state="paused")
            except Exception:
                pass
            return True

        # --- Send chunks ---
        for i, chunk in enumerate(chunks):
            if not self._running:
                break
            if i > 0 and not self._reserve_outbound_slot():
                _logger.warning(
                    "dispatcher rate limit hit mid-send for %s", external_user_id)
                self._emit("throttled", external_user_id, session_id,
                           queue_depth=self._queue_depth(external_user_id),
                           throttle_reason="outbound_quota")
                break

            try:
                delivered = self._channel._do_send(
                    external_user_id, chunk, session_id=session_id)
                if delivered is False:
                    self._emit("failed", external_user_id, session_id,
                               chunk_index=i + 1, chunk_count=len(chunks),
                               queue_depth=self._queue_depth(external_user_id))
                    break
                self._emit("sent", external_user_id, session_id,
                           chunk_index=i + 1, chunk_count=len(chunks),
                           queue_depth=self._queue_depth(external_user_id))
            except Exception:
                _logger.exception(
                    "dispatcher chunk %d/%d failed for %s",
                    i + 1, len(chunks), external_user_id)
                self._emit("failed", external_user_id, session_id,
                           chunk_index=i + 1, chunk_count=len(chunks),
                           queue_depth=self._queue_depth(external_user_id))
                break

            # Inter-chunk pacing
            if i < len(chunks) - 1 and self._running:
                # Small gap between chunks (0.5-1.5s)
                inter_chunk = 0.5 + (secrets.randbelow(1000) / 1000.0)
                self._interruptible_sleep(inter_chunk)

        # --- Cleanup ---
        self._last_send[external_user_id] = time.monotonic()

        try:
            self._channel.send_typing(external_user_id, state="paused")
        except Exception:
            pass

        # Emit observability event (no message body in logs)
        _logger.info(
            "dispatcher sent: user=%s chunks=%d len=%d channel=%s",
            external_user_id, len(chunks), len(text),
            getattr(self._channel, "channel_id", "?"))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _interruptible_sleep(self, seconds: float):
        """Sleep in small ticks so ``shutdown`` can interrupt quickly."""
        tick = 0.1
        elapsed = 0.0
        while elapsed < seconds and self._running:
            time.sleep(min(tick, seconds - elapsed))
            elapsed += tick

    def _reserve_outbound_slot(self) -> bool:
        """Atomically reserve one rolling outbound slot before a send."""
        max_per_minute = self._setting_int("whatsapp_max_outbound_per_minute", "30")
        if max_per_minute <= 0:
            return True

        now = time.monotonic()
        window = 60.0
        with self._rate_limit_lock:
            while self._outbound_window and now - self._outbound_window[0] > window:
                self._outbound_window.popleft()
            if len(self._outbound_window) >= max_per_minute:
                return False
            self._outbound_window.append(now)
            return True

    def _prune_dedup(self, now: float):
        """Remove expired dedup entries."""
        expired = [k for k, v in self._dedup.items()
                     if now - v > self._dedup_ttl]
        for k in expired:
            del self._dedup[k]


# ---------------------------------------------------------------------------
# Text formatting: Markdown → WhatsApp-natural plain text
# ---------------------------------------------------------------------------

def _natural_whatsapp_format(text: str) -> str:
    """Convert markdown-heavy text into WhatsApp-native conversational text.

    This is deterministic and safe — even if the LLM outputs noncompliant
    markup, the result should still be readable.
    """
    import re

    # 1. Fenced code blocks → compact CODE: section
    text = re.sub(
        r'```[^\n]*\n(.*?)```',
        lambda m: 'CODE:\n' + m.group(1).strip(),
        text, flags=re.DOTALL)

    # 2. Inline code → `code` preserved (WhatsApp uses backticks natively)
    #    No change needed.

    # 3. Bold **text** and __text__ → remove markup
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)

    # 4. Italic *text* and _text_ → remove markup
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'\1', text)
    text = re.sub(r'(?<!_)_(?!_)(.+?)(?<!_)_(?!_)', r'\1', text)

    # 5. Strikethrough ~~text~~ → remove markup
    text = re.sub(r'~~(.+?)~~', r'\1', text)

    # 6. Links [label](url) → "label: url"
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1: \2', text)

    # 7. Raw URLs preserved as-is

    # 8. Headings # ## ### etc → short plain labels
    text = re.sub(r'^#{1,2}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^#{3,6}\s+', '', text, flags=re.MULTILINE)

    # 9. Unordered lists: - or * at line start → bullet •
    text = re.sub(r'^[\-\*]\s+', '• ', text, flags=re.MULTILINE)

    # 10. Ordered lists preserved as-is (1. 2. etc)

    # 11. Blockquotes > → indent with "  "
    text = re.sub(r'^>\s?', '  ', text, flags=re.MULTILINE)

    # 12. Horizontal rules --- *** ___ → thin separator
    text = re.sub(r'^[\-\*_]{3,}\s*$', '───', text, flags=re.MULTILINE)

    # 13. Collapse excessive blank lines (3+ → 2)
    text = re.sub(r'\n{3,}', '\n\n', text)

    # 14. Strip leading/trailing whitespace
    text = text.strip()

    return text


# ---------------------------------------------------------------------------
# Message splitting (4096-char WhatsApp limit)
# ---------------------------------------------------------------------------

def _split_message(text: str, max_len: int = 4096) -> list:
    """Split text into chunks within WhatsApp's message size limit."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = -1
        for sep in ('\n\n', '\n', ' '):
            pos = text.rfind(sep, 0, max_len)
            if pos > 0:
                split_at = pos
                break
        if split_at <= 0:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')
    return chunks
