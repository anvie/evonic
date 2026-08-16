"""
monitors — Opt-in condition watchers for background processes and log files.

Background processes are **not** watched by default: spawning one is silent, so
an unrelated `nohup` finishing never derails whatever the agent is doing. When
the outcome actually matters the agent attaches a monitor, and only a monitor
firing produces a notification.

A monitor is one persisted scheduler job (`action_type="poll_monitor"`). All the
state needed to poll lives in its ``action_config`` — including the fully
pre-built probe script — so watching survives a server restart and each tick is
a single ``run_bash`` round-trip into the session backend. Monitors are one-shot:
the scheduler self-cancels the row as soon as the monitor resolves.

A monitor resolves in one of three ways, each of which notifies exactly once:

* **matched**  — a condition in ``when`` was met.
* **ended**    — the watched process exited without ever meeting the condition
                 (the agent asked to be told about this job; silence would leave
                 it waiting forever).
* **expired**  — ``expires_in`` elapsed with nothing to report.

The scheduler *is* the registry — there is no in-memory mirror to lose on
restart. See :func:`list_for_session`.
"""
from __future__ import annotations

import logging
import re
import shlex
import time
from typing import Optional

_logger = logging.getLogger(__name__)

SCHEDULE_OWNER_TYPE = "monitor"
ACTION_TYPE = "poll_monitor"

_DEFAULT_INTERVAL = 30
_MIN_INTERVAL = 10
_MAX_INTERVAL = 600
_DEFAULT_EXPIRES = 6 * 60 * 60           # 6 hours
_MAX_EXPIRES = 24 * 60 * 60
_MAX_PER_SESSION = 10
_POLL_TIMEOUT = 15
_TAIL_LINES = 30
_TAIL_CHARS = 1500

_MARKER = "__EVMON__"
_CONDITION_KEYS = ("on_exit", "on_failure", "log_match", "shell")


# ---------------------------------------------------------------------------
# Probe script
# ---------------------------------------------------------------------------

def build_probe_script(status_script: str, log_file: str, need_exit: bool,
                       log_match: str, shell: str) -> str:
    """Assemble the single shell probe a monitor runs on each tick.

    Emits only the lines the monitor's conditions actually need::

        __EVMON__
        S:RUNNING|DONE      process liveness
        X:<code>            exit code (wrapper spawns only)
        M:1|0               log_match regex hit
        H:1|0               shell predicate exited 0
        T:                  followed by the log tail, to end of output
    """
    q_log = shlex.quote(log_file) if log_file else ""
    lines = [f"echo {_MARKER}"]

    if status_script:
        # Piped rather than nested in $() so the snippet's own quoting can't
        # collide with ours.
        lines.append(f"{{ {status_script} ; }} 2>/dev/null | tail -1 | sed 's/^/S:/'")

    if need_exit and q_log:
        # The guard wrapper appends `EXIT_CODE=<n>` as the last log line.
        lines.append(
            f"tail -n 5 {q_log} 2>/dev/null | "
            f"sed -n 's/^EXIT_CODE=\\([0-9][0-9]*\\)$/X:\\1/p' | tail -1"
        )

    if log_match and q_log:
        lines.append(
            f"grep -Eq -- {shlex.quote(log_match)} {q_log} 2>/dev/null "
            f"&& echo M:1 || echo M:0"
        )

    if shell:
        lines.append(f"{{ {shell} ; }} >/dev/null 2>&1 && echo H:1 || echo H:0")

    if q_log:
        lines.append("echo T:")
        lines.append(f"tail -n {_TAIL_LINES} {q_log} 2>/dev/null")

    return "\n".join(lines)


def parse_probe_output(stdout: str) -> Optional[dict]:
    """Parse probe output into {status, exit_code, matched, shell_ok, tail}.

    Returns None when the marker is absent — the probe did not run cleanly and
    the tick should be treated as inconclusive rather than as a resolution.
    """
    if _MARKER not in (stdout or ""):
        return None

    out = {"status": None, "exit_code": None, "matched": None,
           "shell_ok": None, "tail": ""}
    body = stdout.split(_MARKER, 1)[1].lstrip("\n")
    lines = body.split("\n")

    for i, line in enumerate(lines):
        if line == "T:":
            out["tail"] = "\n".join(lines[i + 1:]).strip()
            break
        if line.startswith("S:"):
            out["status"] = line[2:].strip() or None
        elif line.startswith("X:"):
            val = line[2:].strip()
            out["exit_code"] = int(val) if val.isdigit() else None
        elif line.startswith("M:"):
            out["matched"] = line[2:].strip() == "1"
        elif line.startswith("H:"):
            out["shell_ok"] = line[2:].strip() == "1"

    return out


# ---------------------------------------------------------------------------
# Attach / list / detach
# ---------------------------------------------------------------------------

def _describe(when: dict) -> str:
    parts = []
    if when.get("on_failure"):
        parts.append("process exits non-zero")
    elif when.get("on_exit"):
        parts.append("process exits")
    if when.get("log_match"):
        parts.append(f"log matches /{when['log_match']}/")
    if when.get("shell"):
        parts.append(f"`{when['shell']}` succeeds")
    return " OR ".join(parts)


def _check_shell_safety(agent: dict, snippet: str) -> Optional[str]:
    """Run a shell predicate through the same safety pipeline the bash tool uses.

    Returns an error string when the snippet must not run, else None.
    """
    try:
        from backend.tools.lib.safety_pipeline import (
            get_safety_pipeline, should_skip_safety)
    except ImportError:
        return None
    if should_skip_safety(agent) or agent.get("is_super"):
        return None
    if not agent.get("safety_checker_enabled", 1):
        return None
    try:
        verdict = get_safety_pipeline().check(
            snippet, tool_type="bash", agent_context=agent)
    except Exception:
        return None
    if verdict.get("level") in ("dangerous", "requires_approval"):
        return (f"Shell predicate rejected by the safety system "
                f"({verdict.get('level')}): {'; '.join(verdict.get('reasons') or [])}")
    return None


def attach(agent: dict, target: dict, when: dict, note: str = "",
           interval: int = _DEFAULT_INTERVAL,
           expires_in: int = _DEFAULT_EXPIRES) -> dict:
    """Create a monitor. Returns {'monitor_id', ...} or {'error': ...}."""
    from backend.agent_runtime.background_jobs import (
        background_jobs, build_manual_status_script)
    from backend.tools.lib.long_running_guard import build_status_scripts
    from backend.scheduler import scheduler

    agent = agent or {}
    session_id = agent.get("session_id") or "default"
    agent_id = agent.get("agent_id") or agent.get("id") or ""
    if not agent_id:
        return {"error": "Monitor requires an agent context."}

    active = list_for_session(agent_id, session_id)
    if len(active) >= _MAX_PER_SESSION:
        return {"error": f"This session already has {_MAX_PER_SESSION} monitors "
                         f"(the limit). Detach one first — see action='list'."}

    target = target or {}
    when = {k: v for k, v in (when or {}).items() if v not in (None, "", False)}
    unknown = set(when) - set(_CONDITION_KEYS)
    if unknown:
        return {"error": f"Unknown condition(s): {', '.join(sorted(unknown))}. "
                         f"Supported: {', '.join(_CONDITION_KEYS)}."}
    if not when:
        return {"error": "No condition given. Set at least one of: "
                         f"{', '.join(_CONDITION_KEYS)}."}

    log_match = when.get("log_match") or ""
    if log_match:
        try:
            re.compile(log_match)
        except re.error as e:
            return {"error": f"Invalid log_match regex: {e}"}

    shell = (when.get("shell") or "").strip()
    if shell:
        err = _check_shell_safety(agent, shell)
        if err:
            return {"error": err}

    # -- resolve the target -------------------------------------------------
    job_id = target.get("job_id") or ""
    log_file = (target.get("log_file") or "").strip()
    status_script = ""
    need_exit = False
    kind = ""
    command = ""

    if job_id:
        job = background_jobs.get(job_id)
        if not job:
            known = [j.job_id for j in background_jobs.list_for_session(session_id)]
            return {"error": f"Unknown job_id {job_id!r}. "
                             f"Jobs in this session: {known or 'none'}."}
        if job.session_id != session_id:
            return {"error": f"Job {job_id!r} belongs to another session."}
        kind, command = job.kind, job.command
        log_file = log_file or job.log_file
        if kind == "wrapper":
            status_script = build_status_scripts(
                job.session_name, job.log_file, job.pid_file)["check_status_script"]
        else:
            status_script = build_manual_status_script(
                kind, job.session_name, job.pid_file, job.pgrep_pattern)
        need_exit = kind == "wrapper"
    elif when.get("on_exit") or when.get("on_failure"):
        return {"error": "on_exit/on_failure need target={'job_id': ...} — "
                         "there is no process to watch otherwise."}

    if when.get("on_failure") and job_id and kind != "wrapper":
        return {"error": f"Exit codes are not observable for {kind} spawns, so "
                         f"on_failure cannot work here. Use on_exit, or "
                         f"log_match on an error pattern."}
    if log_match and not log_file:
        return {"error": "log_match needs a log file — pass "
                         "target={'log_file': ...} or a job that has one."}
    if not (status_script or log_match or shell):
        return {"error": "Nothing to watch: give a job_id, a log_file, or a "
                         "shell predicate."}

    try:
        interval = max(_MIN_INTERVAL, min(int(interval), _MAX_INTERVAL))
    except (TypeError, ValueError):
        interval = _DEFAULT_INTERVAL
    try:
        expires_in = max(interval, min(int(expires_in), _MAX_EXPIRES))
    except (TypeError, ValueError):
        expires_in = _DEFAULT_EXPIRES

    probe = build_probe_script(status_script, log_file, need_exit, log_match, shell)

    action_config = {
        "session_id": session_id,
        "agent_id": agent_id,
        "external_user_id": agent.get("user_id"),
        "channel_id": agent.get("channel_id"),
        "job_id": job_id,
        "command": command or (log_file or shell)[:200],
        "log_file": log_file,
        "kind": kind,
        "when": when,
        "note": (note or "")[:500],
        "probe_script": probe,
        "deadline_ts": time.time() + expires_in,
    }

    try:
        sched = scheduler.create_schedule(
            name=f"monitor:{(command or log_file or shell)[:40]}",
            owner_type=SCHEDULE_OWNER_TYPE,
            owner_id=agent_id,
            trigger_type="interval",
            trigger_config={"seconds": interval},
            action_type=ACTION_TYPE,
            action_config=action_config,
            max_runs=(expires_in // interval) + 2,   # backstop if self-cancel fails
            metadata={"session_id": session_id},
        )
    except Exception as e:
        _logger.warning("[monitor] failed to create schedule: %s", e)
        return {"error": f"Could not create the monitor: {e}"}

    if not sched or not sched.get("id"):
        return {"error": "Could not create the monitor."}

    monitor_id = f"mon-{sched['id']}"
    # Stamp the id into the stored config so polls can report it.
    try:
        from models.db import db
        action_config["monitor_id"] = monitor_id
        db.update_schedule(sched["id"], action_config=action_config)
    except Exception as e:
        _logger.warning("[monitor] failed to stamp monitor_id: %s", e)

    return {
        "monitor_id": monitor_id,
        "watching": command or log_file or shell,
        "condition": _describe(when),
        "interval_seconds": interval,
        "expires_in_seconds": expires_in,
        "note": ("Attached. You will be notified once — when the condition is "
                 "met, when the watched process ends without meeting it, or on "
                 "expiry. Do not poll for it."),
    }


def list_for_session(agent_id: str, session_id: str) -> list:
    """Active monitors for this session, read straight from the scheduler."""
    from backend.scheduler import scheduler

    out = []
    try:
        schedules = scheduler.list_schedules(
            owner_type=SCHEDULE_OWNER_TYPE, owner_id=agent_id)
    except Exception as e:
        _logger.warning("[monitor] list failed: %s", e)
        return out

    now = time.time()
    for s in schedules:
        cfg = s.get("action_config") or {}
        if session_id and cfg.get("session_id") != session_id:
            continue
        out.append({
            "monitor_id": cfg.get("monitor_id") or f"mon-{s.get('id')}",
            "job_id": cfg.get("job_id") or None,
            "watching": cfg.get("command") or cfg.get("log_file") or "",
            "condition": _describe(cfg.get("when") or {}),
            "note": cfg.get("note") or "",
            "expires_in_seconds": max(0, int((cfg.get("deadline_ts") or now) - now)),
        })
    return out


def monitored_job_ids(agent_id: str, session_id: str) -> set:
    """job_ids in this session that currently have a monitor attached."""
    return {m["job_id"] for m in list_for_session(agent_id, session_id)
            if m.get("job_id")}


def detach(agent_id: str, monitor_id: str) -> dict:
    from backend.scheduler import scheduler

    schedule_id = (monitor_id or "").strip()
    if schedule_id.startswith("mon-"):
        schedule_id = schedule_id[4:]
    if not schedule_id:
        return {"error": "monitor_id is required."}

    try:
        ok = scheduler.cancel_schedule(schedule_id, owner_id=agent_id)
    except Exception as e:
        _logger.warning("[monitor] detach failed: %s", e)
        return {"error": f"Could not detach: {e}"}
    if not ok:
        return {"error": f"No monitor {monitor_id!r} found for this agent."}
    return {"status": "detached", "monitor_id": monitor_id}


# ---------------------------------------------------------------------------
# Poll tick (called by the scheduler)
# ---------------------------------------------------------------------------

def run_monitor_poll(action_config: dict) -> dict:
    """Evaluate one monitor. Returns {'done': bool, 'state': str}.

    ``done=True`` tells the scheduler to cancel the schedule — the monitor has
    resolved (fired, the process ended, or it expired) and the agent has been
    notified. Inconclusive ticks (backend down, probe garbled) return False so
    the next interval retries.
    """
    from backend.tools.lib.exec_backend import registry
    from models.db import db

    session_id = action_config.get("session_id") or "default"
    when = action_config.get("when") or {}
    deadline_ts = action_config.get("deadline_ts") or 0

    if deadline_ts and time.time() > deadline_ts:
        _notify(action_config, "expired", "")
        return {"done": True, "state": "expired"}

    agent = db.get_agent(action_config.get("agent_id")) or {}
    try:
        backend = registry.get_backend(session_id, agent)
    except Exception as e:
        _logger.warning("[monitor] backend resolve failed: %s", e)
        return {"done": False, "state": "backend_error"}

    try:
        res = backend.run_bash(action_config.get("probe_script") or "",
                               _POLL_TIMEOUT, {})
    except Exception as e:
        _logger.warning("[monitor] probe failed: %s", e)
        return {"done": False, "state": "probe_error"}

    probe = parse_probe_output(res.get("stdout") or "")
    if probe is None:
        return {"done": False, "state": "probe_unparsed"}

    ended = probe["status"] == "DONE"
    exit_code = probe["exit_code"]

    if probe["shell_ok"]:
        return _resolve(action_config, "matched", probe, "shell predicate succeeded")
    if probe["matched"]:
        return _resolve(action_config, "matched", probe,
                        f"log matched /{when.get('log_match')}/")
    if ended and when.get("on_failure") and exit_code not in (0, None):
        return _resolve(action_config, "matched", probe,
                        f"process failed (exit code {exit_code})")
    if ended and when.get("on_exit"):
        code = "unknown" if exit_code is None else exit_code
        return _resolve(action_config, "matched", probe,
                        f"process finished (exit code {code})")
    if ended:
        # Watched process is gone and its condition can no longer be met.
        return _resolve(action_config, "ended", probe,
                        "process ended without meeting the condition")

    return {"done": False, "state": "waiting"}


def _resolve(action_config: dict, state: str, probe: dict, detail: str) -> dict:
    job_id = action_config.get("job_id")
    if job_id and probe.get("status") == "DONE":
        from backend.agent_runtime.background_jobs import background_jobs
        background_jobs.mark_finished(job_id, "done", probe.get("exit_code"))
    _notify(action_config, state, detail, tail=probe.get("tail") or "")
    return {"done": True, "state": state}


def _notify(action_config: dict, state: str, detail: str, tail: str = "") -> None:
    """Deliver the monitor result to the agent via the central notifier."""
    command = action_config.get("command") or "the watched target"
    log_file = action_config.get("log_file") or ""
    note = action_config.get("note") or ""

    if state == "expired":
        headline = (f"Monitor on `{command}` expired without firing — it stopped "
                    f"watching. Re-attach it if you still need the result.")
    elif state == "ended":
        headline = (f"Monitor on `{command}` stopped: {detail}. Nothing to "
                    f"report beyond that.")
    else:
        headline = f"Monitor fired on `{command}` — {detail}."

    body = [
        headline,
        "",
        "There is no user message to answer; report this proactively. The "
        "monitor has already been removed, so no cleanup is needed.",
    ]
    if note:
        body.append(f"\nYou attached it with the note: {note}")
    if log_file:
        body.append(f"Log file: {log_file}")
    if tail:
        body.append(f"\nLast output:\n```\n{tail[-_TAIL_CHARS:]}\n```")
    body.append(
        "\nFollow up appropriately: verify or summarize the outcome for the "
        "user concisely. If it failed, note the likely cause and a next step."
    )

    try:
        from backend.agent_runtime.notifier import notify_agent
        notify_agent(
            agent_id=action_config.get("agent_id"),
            tag="MONITOR",
            message="\n".join(body),
            external_user_id=action_config.get("external_user_id"),
            channel_id=action_config.get("channel_id"),
            dedup=False,
            trigger_llm=True,
            metadata={"monitor_id": action_config.get("monitor_id"),
                      "monitor_state": state},
        )
    except Exception as e:
        _logger.warning("[monitor] notify failed: %s", e)
