"""
In-place server restart — single source of truth.

Restarts the running server by replacing the current process image with a fresh
one (os.execv), preserving the PID the run/ lock and PID file point to. This is
the only restart mechanism that works without an external process manager, so it
is correct under the documented `evonic start -d` install path as well as under
systemd/Docker.

Used by the /restart slash command, the super-agent restart tool, the web setup
auto-restart, and the web update "Restart" action so every restart path behaves
identically.
"""

import logging
import os
import sys
import threading
import time

log = logging.getLogger(__name__)


def restart_in_place(delay: float = 1.5) -> None:
    """Stop channels and the scheduler, free inherited FDs, and re-exec the server.

    Runs in the calling thread and never returns on success (the process image is
    replaced). Intended to be invoked from a daemon thread via schedule_restart().
    """
    time.sleep(delay)  # Let the triggering response/log flush first.

    # Stop all channels cleanly so Telegram releases its long-poll before the
    # new process re-opens them (otherwise the bot token hits a getUpdates
    # Conflict on the next boot).
    try:
        from backend.channels.registry import channel_manager
        channel_manager.stop_all()
        time.sleep(1.0)  # Give Telegram server-side time to release.
    except Exception as e:
        log.error("Error stopping channels during restart: %s", e, exc_info=True)

    # Shut down the scheduler so in-flight jobs are not abandoned mid-execution.
    try:
        from backend.scheduler import scheduler as global_scheduler
        global_scheduler.shutdown()
    except Exception as e:
        log.error("Error shutting down scheduler during restart: %s", e, exc_info=True)

    # Close inherited file descriptors (including Flask's bound socket) so the
    # new process can re-bind the same port. close_range(inheritable=False) keeps
    # the FDs in this process but stops them leaking into the new image; fall back
    # to closerange on older Python. POSIX-only — skip on Windows.
    if sys.platform != 'win32':
        try:
            import resource
            maxfd = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
            if maxfd == resource.RLIM_INFINITY or maxfd > 65535:
                maxfd = 4096
            try:
                os.close_range(3, maxfd, inheritable=False)
            except AttributeError:
                os.closerange(3, maxfd)
        except Exception:
            pass

    # Flat-repo architecture: project root IS the live directory.
    import config
    target = os.path.realpath(config.BASE_DIR)
    app_py = os.path.join(target, 'app.py')
    venv_python = os.path.join(target, '.venv', 'bin', 'python')
    python = venv_python if os.path.exists(venv_python) else sys.executable

    log.info("Re-executing server process")
    os.chdir(target)
    os.execv(python, [python, app_py])


def schedule_restart(delay: float = 1.5) -> None:
    """Run restart_in_place() in a daemon thread and return immediately."""
    threading.Thread(target=restart_in_place, args=(delay,), daemon=True).start()
