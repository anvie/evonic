"""
background_jobs — Identify and track background processes started by an agent.

Background processes reach this module two ways:

1. **Guard wrappers** — a build/download launched via the long_running_guard
   wrapper (tmux/screen/nohup with log + EXIT_CODE marker).
2. **Manual spawns** — the agent's own ``tmux new-session -d``, ``screen -dmS``
   or ``nohup … &`` scripts, detected by :func:`parse_manual_spawn`.

Both are registered by the bash tool after a successful spawn. Registration is
**silent**: a background process is never watched and never notifies anyone on
its own. It is visible on demand (the ``/jobs`` command, the Session State
panel, ``tail`` via bash), and the agent attaches a monitor to the returned
``job_id`` when — and only when — the outcome actually matters. See
:mod:`backend.agent_runtime.monitors`.

The registry here is in-memory and transient: it holds job identity + status for
the Session State UI and to resolve ``job_id`` when a monitor is attached.
Monitors keep everything they need in their own persisted schedule, so losing
this registry on restart only blanks the UI list.
"""
from __future__ import annotations

import logging
import threading
import time
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

_logger = logging.getLogger(__name__)

# Finished jobs kept per session for the Session State UI (oldest pruned).
_MAX_FINISHED_PER_SESSION = 10


@dataclass
class BackgroundJob:
    job_id: str
    session_id: str
    session_name: str
    log_file: str
    pid_file: str
    command: str
    started_at: float
    kind: str = "wrapper"            # wrapper | tmux | screen | nohup
    pgrep_pattern: str = ""          # nohup fallback when no PID file
    status: str = "running"          # running | done | timeout
    exit_code: Optional[int] = None
    finished_at: Optional[float] = None


class BackgroundJobRegistry:
    """Thread-safe in-memory registry of background jobs (running + recent)."""

    def __init__(self):
        self._jobs: Dict[str, BackgroundJob] = {}
        self._guard = threading.Lock()
        self._counter = 0

    def register(self, session_id: str, session_name: str, log_file: str,
                 pid_file: str, command: str, kind: str = "wrapper",
                 pgrep_pattern: str = "") -> BackgroundJob:
        with self._guard:
            dedup_key = session_name or pgrep_pattern or command
            for j in self._jobs.values():
                if j.session_id == session_id and j.status == "running" and \
                        (j.session_name or j.pgrep_pattern or j.command) == dedup_key:
                    return j  # dedup: same spawn re-run
            self._counter += 1
            job = BackgroundJob(
                job_id=f"job{self._counter}",
                session_id=session_id,
                session_name=session_name,
                log_file=log_file,
                pid_file=pid_file,
                command=command,
                started_at=time.time(),
                kind=kind,
                pgrep_pattern=pgrep_pattern,
            )
            self._jobs[job.job_id] = job
            self._prune_finished(session_id)
            _logger.info("[bgjob] registered %s kind=%s session=%s sess=%s cmd=%r",
                         job.job_id, kind, session_id, session_name, command)
            return job

    def _prune_finished(self, session_id: str) -> None:
        """Drop oldest finished jobs beyond the per-session cap. Caller holds lock."""
        finished = sorted(
            [j for j in self._jobs.values()
             if j.session_id == session_id and j.status != "running"],
            key=lambda j: j.finished_at or 0,
        )
        excess = len(finished) - _MAX_FINISHED_PER_SESSION
        for j in finished[:max(0, excess)]:
            self._jobs.pop(j.job_id, None)

    def mark_finished(self, job_id: str, status: str,
                      exit_code: Optional[int]) -> None:
        """Record completion for UI display. No-op if the job is gone (restart)."""
        with self._guard:
            j = self._jobs.get(job_id)
            if not j or j.status != "running":
                return
            j.status = status
            j.exit_code = exit_code
            j.finished_at = time.time()
            self._prune_finished(j.session_id)

    def get(self, job_id: str) -> Optional[BackgroundJob]:
        with self._guard:
            return self._jobs.get(job_id)

    def running_for_session(self, session_id: str) -> List[BackgroundJob]:
        with self._guard:
            return [j for j in self._jobs.values()
                    if j.session_id == session_id and j.status == "running"]

    def list_for_session(self, session_id: str) -> List[BackgroundJob]:
        with self._guard:
            return [j for j in self._jobs.values() if j.session_id == session_id]


# Singleton
background_jobs = BackgroundJobRegistry()


def parse_wrapper_script(script: str) -> Optional[dict]:
    """Extract job identity from a long_running_guard wrapper script.

    Returns dict(session_name, log_file, pid_file, command) when the script is a
    generated wrapper (starts with the bypass marker), else None.
    """
    from backend.tools.lib.long_running_guard import BYPASS_MARKER

    if not script.lstrip().startswith(BYPASS_MARKER):
        return None

    def _grab(pattern: str) -> str:
        m = re.search(pattern, script)
        return m.group(1) if m else ""

    session_name = _grab(r'SESS="([^"]+)"')
    if not session_name:
        return None
    log_file = _grab(r'LOG_FILE="([^"]+)"')
    pid_file = _grab(r'PID_FILE="([^"]+)"')

    cmd_m = re.search(r"SCRIPT_CMD='\{ (.*?); \}; EC=\$\?", script, re.DOTALL)
    command = cmd_m.group(1).strip() if cmd_m else session_name
    command = command.replace("'\\''", "'")  # undo wrapper's quote escaping

    return {
        "session_name": session_name,
        "log_file": log_file,
        "pid_file": pid_file,
        "command": command,
    }


_TMUX_SPAWN_RE = re.compile(r'\btmux\s+(?:new-session|new)\b(.*)', re.DOTALL)
_SCREEN_SPAWN_RE = re.compile(r'\bscreen\s+(.*)', re.DOTALL)
_NOHUP_LINE_RE = re.compile(r'^\s*nohup\s+(.+)$', re.MULTILINE)
_PID_CAPTURE_RE = re.compile(r'echo\s+\$!\s*>>?\s*(\S+)')


def _trim_unquoted(text: str) -> str:
    """Cut ``text`` at the first ``;``/``|``/``&``/newline outside quotes.

    Those metacharacters end the spawn only when the shell sees them — inside a
    quoted argument they belong to the command being run, and cutting on them
    truncates the command mid-string.
    """
    quote = ''
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '\\' and quote != "'":
            i += 2  # escaped char (backslash is literal inside single quotes)
            continue
        if quote:
            if ch == quote:
                quote = ''
        elif ch in '"\'':
            quote = ch
        elif ch in ';|&\n':
            return text[:i]
        i += 1
    return text


def parse_manual_spawn(script: str) -> Optional[dict]:
    """Detect an agent-authored background spawn (tmux/screen/nohup).

    Returns dict(kind, session_name, log_file, pid_file, pgrep_pattern, command)
    for the first spawn found, or None. Guard wrapper scripts (BYPASS_MARKER)
    are excluded — those go through :func:`parse_wrapper_script`.
    """
    from backend.tools.lib.long_running_guard import BYPASS_MARKER

    if script.lstrip().startswith(BYPASS_MARKER):
        return None

    # Join backslash-newline continuations so multi-line spawn commands are
    # captured in full — the spawn regexes stop at a literal newline.
    script = re.sub(r'\\\n\s*', ' ', script)

    # tmux new-session -d -s NAME 'cmd'
    m = _TMUX_SPAWN_RE.search(script)
    if m:
        seg = _trim_unquoted(m.group(1))
        detached = re.search(r'\s-[A-Za-z]*d', ' ' + seg)
        name_m = re.search(r'-[A-Za-z]*s\s+["\']?([^\s"\';|&]+)', seg)
        if detached and name_m:
            return {
                "kind": "tmux",
                "session_name": name_m.group(1),
                "log_file": "",
                "pid_file": "",
                "pgrep_pattern": "",
                "command": (script[m.start():m.start(1)] + seg).strip()[:1000],
            }

    # screen -dmS NAME cmd  (or -d -m -S NAME)
    m = _SCREEN_SPAWN_RE.search(script)
    if m:
        seg = _trim_unquoted(m.group(1))
        name_m = re.search(r'-(?:[A-Za-z]*S)\s+["\']?([^\s"\';|&]+)', seg)
        detached = '-dmS' in seg or ('-d' in seg and '-m' in seg)
        if detached and name_m:
            return {
                "kind": "screen",
                "session_name": name_m.group(1),
                "log_file": "",
                "pid_file": "",
                "pgrep_pattern": "",
                "command": (script[m.start():m.start(1)] + seg).strip()[:1000],
            }

    # nohup CMD [> log 2>&1] &  [echo $! > pidfile]
    m = _NOHUP_LINE_RE.search(script)
    if m:
        line = re.sub(r'2>&1', '', m.group(1))
        # Backgrounded = a lone '&' (not '&&'); may sit mid-line before
        # e.g. 'echo $! > pidfile'. Foreground nohup blocks the bash call
        # itself, so there is nothing to watch.
        amp = re.search(r'(?<!&)&(?!&)', line)
        if not amp:
            return None
        before_amp = line[:amp.start()]
        # Command text: everything up to the first redirect
        command = re.split(r'\s[12]?>>?', before_amp)[0].strip()
        if not command:
            return None
        # stdout redirect target (ignore stderr-only redirects like 2>file)
        line_wo_stderr = re.sub(r'2>>?\s*\S+', '', before_amp)
        log_m = re.search(r'>>?\s*([^\s&]+)', line_wo_stderr)
        pid_m = _PID_CAPTURE_RE.search(script)
        return {
            "kind": "nohup",
            "session_name": "",
            "log_file": log_m.group(1) if log_m else "",
            "pid_file": pid_m.group(1) if pid_m else "",
            "pgrep_pattern": "" if pid_m else command[:80].strip('"\''),
            "command": ("nohup " + command)[:1000],
        }

    return None


def build_manual_status_script(kind: str, session_name: str, pid_file: str,
                               pgrep_pattern: str) -> str:
    """RUNNING/DONE poll snippet for a manually spawned background process."""
    if kind == "tmux":
        return (f'tmux has-session -t {session_name} 2>/dev/null '
                f'&& echo "RUNNING" || echo "DONE"')
    if kind == "screen":
        return (f'screen -list 2>/dev/null | grep -q {session_name} '
                f'&& echo "RUNNING" || echo "DONE"')
    if pid_file:
        return (f'[ -f {pid_file} ] && kill -0 $(cat {pid_file}) 2>/dev/null '
                f'&& echo "RUNNING" || echo "DONE"')
    esc = pgrep_pattern.replace("'", "'\\''")
    return (f"pgrep -f -- '{esc}' >/dev/null 2>&1 "
            f'&& echo "RUNNING" || echo "DONE"')


# Max jobs listed in the per-turn context block; the rest collapse to a count.
_MAX_IN_CONTEXT = 8
# Command chars kept per line — the full text stays in the Session State panel.
_CONTEXT_CMD_CHARS = 70


def _age(seconds: float) -> str:
    secs = int(max(0, seconds))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def build_context_block(session_id: str, agent_id: str) -> str:
    """Per-turn context block listing this session's running background jobs.

    The registry is otherwise invisible to the agent: the ``background_job``
    field on a bash result is seen once, at spawn time, and slash command output
    never enters LLM context. Without this block the agent forgets processes it
    started and leaves them to go stale.

    Returns "" when nothing is running, so a session with no jobs pays nothing.
    Status is whatever was last recorded — deliberately not refreshed here, as
    that costs a shell round-trip per turn (see :func:`refresh_statuses`).
    """
    jobs = sorted(background_jobs.running_for_session(session_id),
                  key=lambda j: j.started_at)
    if not jobs:
        return ""

    try:
        from backend.agent_runtime import monitors
        watched = monitors.monitored_job_ids(agent_id, session_id)
    except Exception as e:
        _logger.warning("[bgjob] monitor lookup for context block failed: %s", e)
        watched = set()

    now = time.time()
    lines = []
    for j in jobs[:_MAX_IN_CONTEXT]:
        cmd = " ".join((j.command or "").split())
        if len(cmd) > _CONTEXT_CMD_CHARS:
            cmd = cmd[:_CONTEXT_CMD_CHARS - 1] + "…"
        flag = "monitored" if j.job_id in watched else "unmonitored"
        lines.append(f"- `{j.job_id}` · {_age(now - j.started_at)} · {flag} — {cmd}")
    if len(jobs) > _MAX_IN_CONTEXT:
        lines.append(f"- …and {len(jobs) - _MAX_IN_CONTEXT} more")

    plural = "es" if len(jobs) != 1 else ""
    return (
        "## Background Processes\n\n"
        f"{len(jobs)} process{plural} you started {'are' if len(jobs) != 1 else 'is'} "
        "still running in this session:\n\n"
        + "\n".join(lines) +
        "\n\nThese keep running until killed. If a process no longer matters, kill "
        "it. If its outcome matters, attach a monitor — nothing will notify you "
        "otherwise. Status is from the last check, not live; verify with bash if "
        "it matters."
    )


_refresh_guard = threading.Lock()
_last_refresh: Dict[str, float] = {}   # session_id -> monotonic-ish timestamp
_refresh_inflight: set = set()         # sessions with a refresh thread running


def refresh_statuses(session_id: str, agent: dict) -> None:
    """Probe every running job of a session in one round-trip; update statuses.

    Synchronous and unthrottled — callers own the pacing. The ``/jobs`` command
    calls it directly; browser-polled callers go through
    :func:`refresh_statuses_async`.
    """
    from backend.tools.lib.exec_backend import registry
    from backend.tools.lib.long_running_guard import build_status_scripts

    jobs = background_jobs.running_for_session(session_id)
    if not jobs:
        return

    lines = []
    for j in jobs:
        if j.kind == "wrapper":
            probe = build_status_scripts(
                j.session_name, j.log_file, j.pid_file)["check_status_script"]
        else:
            probe = build_manual_status_script(
                j.kind, j.session_name, j.pid_file, j.pgrep_pattern)
        lines.append(f"{{ {probe} ; }} 2>/dev/null | tail -1 | sed 's/^/{j.job_id}:/'")

    try:
        backend = registry.get_backend(session_id, agent or {})
        res = backend.run_bash("\n".join(lines), 15, {})
    except Exception as e:
        _logger.warning("[bgjob] status refresh failed: %s", e)
        return

    for line in (res.get("stdout") or "").splitlines():
        job_id, _, state = line.partition(":")
        if state.strip() == "DONE":
            background_jobs.mark_finished(job_id.strip(), "done", None)


def refresh_statuses_async(session_id: str, agent: dict,
                           max_age: float = 10.0) -> None:
    """Fire-and-forget :func:`refresh_statuses` for a browser-polled caller.

    Without this, a job that exits is only ever noticed by the ``/jobs``
    command or by an attached monitor — so an unmonitored process stayed
    ``running`` in the registry forever, kept a live row in the Session State
    panel and was re-stated to the agent every turn.

    The probe is a shell round-trip, so it must not block the request: the
    caller returns the current snapshot and the panel converges on its next
    poll. One thread per session at a time.
    """
    if not background_jobs.running_for_session(session_id):
        return

    with _refresh_guard:
        if session_id in _refresh_inflight:
            return
        if time.time() - _last_refresh.get(session_id, 0.0) < max_age:
            return
        _refresh_inflight.add(session_id)

    def _run():
        try:
            refresh_statuses(session_id, agent)
        except Exception as e:
            _logger.warning("[bgjob] async status refresh failed: %s", e)
        finally:
            with _refresh_guard:
                _last_refresh[session_id] = time.time()
                _refresh_inflight.discard(session_id)

    threading.Thread(target=_run, daemon=True,
                     name=f"bgjob-refresh-{session_id[:8]}").start()
