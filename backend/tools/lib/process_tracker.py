"""
process_tracker — Global registry of running subprocesses per session.

Allows request_stop() to kill a long-running tool immediately by session_id,
regardless of which backend (Docker, Local, SSH) is executing it.

Supports backend-specific killing strategies:
- **Docker**: Use ``container_id`` to kill orphan processes inside the
  container after the exec process is terminated.
- **Local**: Use ``kill_method='killpg_immediate'`` for Bash to kill the
  entire process group (parent + all children) before reaping the leader.
  The legacy ``'killpg'`` strategy retains its parent-first behavior.
- **SSH**: No special handling needed; the existing SSH backend's ``.kill()``
  method already handles remote cleanup.

Usage:
    from backend.tools.lib.process_tracker import process_tracker

    # Docker backend — pass container_id for orphan cleanup
    process_tracker.register(session_id, proc, pid, container_id=cid)

    # Local Bash — signal its dedicated process group before reaping the leader
    process_tracker.register(session_id, proc, pid,
                             kill_method='killpg_immediate')

    try:
        ...  # polling loop
    finally:
        process_tracker.unregister(session_id)

    # From request_stop():
    process_tracker.kill(session_id)
"""

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class ProcessTracker:
    """Thread-safe registry of running subprocesses, keyed by session_id.

    Supports optional ``container_id`` (for Docker orphan cleanup) and
    ``kill_method`` (e.g. ``'killpg_immediate'`` for immediate local Bash
    process-group killing).
    """

    # How long a stop marker stays "pending" after kill() — long enough to
    # catch a subprocess that registers microseconds after a stop, short
    # enough to auto-heal if clear_stop() is never called.
    _STOP_PENDING_TTL = 5.0

    def __init__(self):
        self._processes: dict = {}
        self._stop_pending: dict = {}
        self._lock = threading.Lock()

    def mark_stop(self, session_id: str) -> None:
        """Record that a stop was requested for *session_id* (time-boxed).

        Used to abort a subprocess that starts in the tiny window between
        :meth:`kill` running and the backend calling :meth:`register`.
        """
        with self._lock:
            self._stop_pending[session_id] = __import__('time').time()

    def clear_stop(self, session_id: str) -> None:
        """Clear a pending-stop marker (called when a new request legitimately starts)."""
        with self._lock:
            self._stop_pending.pop(session_id, None)

    def is_stop_pending(self, session_id: str) -> bool:
        """Return True if a stop was requested for *session_id* within the TTL window."""
        with self._lock:
            ts = self._stop_pending.get(session_id)
            if ts is None:
                return False
            if __import__('time').time() - ts > self._STOP_PENDING_TTL:
                self._stop_pending.pop(session_id, None)
                return False
            return True

    def register(self, session_id: str, proc, pid: int,
                 container_id: str = None, kill_method: str = None) -> None:
        """Store a running subprocess for a session.

        Args:
            session_id: The chat session ID.
            proc: A subprocess.Popen object (for Docker/Local) or any object
                  with a .kill() method (for SSH).
            pid: The process PID (or remote PID for SSH).
            container_id: Optional Docker container ID; used during kill() to
                clean up orphan processes inside the container.
            kill_method: Optional killing strategy. Use ``'killpg_immediate'``
                for a dedicated local Bash process group, or ``'killpg'`` for
                process-group cleanup after the leader has been reaped.
        """
        with self._lock:
            self._processes[session_id] = {
                'proc': proc,
                'pid': pid,
                'started_at': __import__('time').time(),
                'container_id': container_id,
                'kill_method': kill_method,
            }

    def is_registered(self, session_id: str) -> bool:
        """Return True if a process is currently registered for *session_id*."""
        with self._lock:
            return session_id in self._processes

    def unregister(self, session_id: str) -> None:
        """Remove the process entry after execution completes naturally."""
        with self._lock:
            self._processes.pop(session_id, None)

    def kill(self, session_id: str) -> None:
        """Terminate and kill the running process for a session.

        Safe to call even if the process has already finished or was never
        registered (no-op for missing entries).

        Applies backend-specific cleanup:

        - If ``container_id`` was provided at registration, runs
          ``docker exec <id> sh -c 'kill -9 -1'`` to kill any orphan
          processes still running inside the Docker container.
        - If ``kill_method='killpg_immediate'`` was provided, sends SIGKILL to
          the dedicated process group before waiting for its leader.
        - If ``kill_method='killpg'`` was provided, sends SIGKILL to the
          process group after the existing parent termination sequence.
        """
        # Mark the stop as pending FIRST so a subprocess registering in the
        # tiny race window right after this call is aborted before it runs.
        self.mark_stop(session_id)

        with self._lock:
            info = self._processes.pop(session_id, None)
        if info is None:
            return
        proc = info['proc']
        pid = info['pid']
        kill_method = info.get('kill_method')

        # --- Effective in-container kill FIRST (Docker) ---
        # Killing the `docker exec` CLIENT proc below does NOT stop the bash
        # running INSIDE the container — it becomes an orphan reparented to
        # PID 1. The step that actually halts a polling/waiting script is
        # ``kill -9 -1`` inside the container, so run it up-front for a
        # near-instant stop. ``kill -9 -1`` SIGKILLs every process except
        # PID 1 (the sleep-infinity sentinel), so the container stays alive.
        container_id = info.get('container_id')
        if container_id:
            try:
                __import__('subprocess').run(
                    ['docker', 'exec', container_id, 'sh', '-c',
                     'kill -9 -1 2>/dev/null || true'],
                    timeout=3,
                )
            except Exception:
                pass  # Best-effort — the client reap below is the fallback

        # Local Bash starts a new session, making its tracked leader PID the
        # process-group ID. Kill that dedicated group before any parent-only
        # grace period so background children cannot continue for two seconds.
        if kill_method == 'killpg_immediate':
            try:
                __import__('os').killpg(pid, __import__('signal').SIGKILL)
            except (ProcessLookupError, OSError):
                pass  # The process group already exited
            try:
                proc.wait(timeout=2)
            except __import__('subprocess').TimeoutExpired:
                # The group signal should include the leader; retain a
                # parent-only fallback for unusual Popen implementations.
                proc.kill()
                proc.wait(timeout=2)
            except Exception as e:
                logger.warning(
                    '[process_tracker] Error reaping process-group leader pid=%s '
                    'for session %s: %s', pid, session_id[:12], e,
                )
            return

        try:
            logger.info(
                '[process_tracker] Killing pid=%s for session %s',
                pid, session_id[:12],
            )
            # If the object has its own .kill() method (e.g. SSH backend),
            # delegate to it.
            if hasattr(proc, 'kill') and not isinstance(proc, __import__('subprocess').Popen):
                proc.kill()
            else:
                # Standard subprocess.Popen: terminate, wait, then kill
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except __import__('subprocess').TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        except Exception as e:
            logger.warning(
                '[process_tracker] Error killing pid=%s for session %s: %s',
                pid, session_id[:12], e,
            )

        # If kill_method is 'killpg', kill the entire process group.
        # This ensures that for local backends the parent bash process
        # and all its children (e.g. sleep, background jobs) are
        # terminated together.
        if kill_method == 'killpg':
            try:
                __import__('os').killpg(info['pid'], __import__('signal').SIGKILL)
            except (ProcessLookupError, OSError):
                pass  # Process already gone


# Module-level singleton
process_tracker = ProcessTracker()
