"""
simulation_runtime — ephemeral, row-less agent-template simulation runs.

A *simulation* instantiates a template's FULL agent (tools, skills, variables,
prompt, KB) inside a throwaway sandbox so the editor's "test conversation" can
exercise the REAL tools with full fidelity — while guaranteeing that **nothing
durable or externally-visible** really happens:

* **No DB row.**  The ephemeral agent lives only in the in-memory registry
  (:meth:`backend.subagent_manager.SubAgentManager.register_ephemeral`).  The
  runtime's ``handle_message`` already falls back to that registry when
  ``db.get_agent`` returns None, so no ``agents`` row is ever inserted.
* **No live files.**  The sim agent's id is registered so the per-agent chat DB
  / chatlog / llm-trace are routed to ``/tmp`` (sub-agent routing), and the
  workspace, KB and artifacts land under
  ``/tmp/evonic_agent_template_simulation/<sim_id>/`` (task #24 containment,
  keyed on ``agent['simulation_id']``).  Teardown removes both trees.
* **No outbound side effects.**  Task #25's interceptor short-circuits
  schedules / messages / memory / kanban / remote calls to synthetic successes
  and records them in a *simulated outbox* the UI can display.
* **Forced sandbox.**  ``simulation_id`` makes the backend registry pick an
  isolating docker/bwrap backend regardless of the template's flags (task #24).

The workspace + KB are materialised eagerly so the agent can read them through
the ``/_self/`` virtual paths, then the whole run is wrapped in a
:class:`SimulationSession` context manager that tears everything down on
success, error, *and* generator/SSE close.  A periodic orphan sweeper reaps any
``/tmp/evonic_agent_template_simulation/*`` tree left behind by a hard crash.

Public entry point: :func:`simulate`.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)

#: Root under which every simulation workspace is created (mirrors
#: :data:`backend.tools.lib.simulation_scope.SIM_ROOT_BASE`).
SIM_ROOT_BASE = "/tmp/evonic_agent_template_simulation"
#: Root the sub-agent machinery routes per-agent sidecar files to under /tmp.
_SUB_AGENT_TMP = "/tmp/evonic-sub-agents"

#: A finished/abandoned simulation tree older than this is reaped by the
#: orphan sweeper (a live run keeps its tree fresh via ``_touch``).
DEFAULT_ORPHAN_TTL = 600.0
#: How often the background orphan sweeper runs when started.
DEFAULT_SWEEP_INTERVAL = 300.0

#: sim_id -> SimulationSession (live runs only).
_sessions: Dict[str, "SimulationSession"] = {}
_sessions_lock = threading.Lock()

_sweeper_thread: Optional[threading.Thread] = None
_sweeper_stop = threading.Event()
_sweeper_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _sim_scope():
    from backend.tools.lib import simulation_scope
    return simulation_scope


def _slug(value: str, limit: int = 20) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (value or ""))
    return (safe[:limit].strip("_") or "tpl")


def new_simulation_id(template_id: str = "tpl") -> str:
    """Return a fresh, collision-resistant simulation id."""
    return f"sim-{_slug(template_id)}-{uuid.uuid4().hex[:10]}"


def sim_root_for(sim_id: str) -> str:
    return os.path.join(SIM_ROOT_BASE, sim_id)


def is_active(sim_id: str) -> bool:
    with _sessions_lock:
        return sim_id in _sessions


def active_simulation_ids() -> List[str]:
    with _sessions_lock:
        return list(_sessions)


# ---------------------------------------------------------------------------
# Spec building (template layer -> row-less agent dict)
# ---------------------------------------------------------------------------

def _merge_variables(template: Dict[str, Any],
                     override_variables: Optional[Any],
                     params: Dict[str, Any]) -> Dict[str, str]:
    """Flatten template variable declarations + overrides into ``{name: value}``.

    Declarations may carry a ``default``; explicit overrides win.  Values are
    stringified so the runtime can inject them as environment variables exactly
    like DB-backed agent variables.
    """
    out: Dict[str, str] = {}
    for decl in (template.get("variables") or []):
        if not isinstance(decl, dict):
            continue
        name = decl.get("name") or decl.get("key")
        if not name:
            continue
        value = decl.get("default")
        if value is None and name in params:
            value = params[name]
        out[str(name)] = "" if value is None else str(value)
    if isinstance(override_variables, dict):
        for key, value in override_variables.items():
            out[str(key)] = "" if value is None else str(value)
    return out


def build_agent_spec(template_id: str,
                     params: Optional[Dict[str, Any]] = None,
                     overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build a NON-persisting agent spec from a template.

    Reuses the template layer (T2) for rendering and the factory (T1) for
    normalisation — but never touches the DB or the filesystem.  Returns a dict
    with the resolved spec plus the fields the simulator needs.
    """
    from backend import agent_templates as tpl
    from backend import agent_factory as factory

    template = tpl.get_template(template_id)
    preview = tpl.preview_template(template_id, params or {})
    resolved_params = preview.get("values") or {}
    overrides = dict(overrides or {})

    tools = list(overrides.get("tools") or template.get("tools") or [])
    skills = list(overrides.get("skills") or template.get("skills") or [])
    kb_files = overrides.get("knowledge_base")
    if kb_files is None:
        kb_files = [{"path": path, "content": content}
                    for path, content in (template.get("kb_files") or {}).items()]

    variables = _merge_variables(template, overrides.get("variables"), resolved_params)

    # Merge defaults + overrides into the spec the factory understands.
    spec: Dict[str, Any] = dict(template.get("defaults") or {})
    for key, value in overrides.items():
        spec[key] = value

    if not spec.get("name"):
        spec["name"] = template.get("name") or template.get("id") or template_id
    if "description" not in spec:
        spec["description"] = template.get("description") or ""
    spec["system_prompt"] = preview.get("system_prompt") or ""
    spec["tools"] = tools
    spec["skills"] = skills
    # The factory expects variable rows ([{key, value, is_secret}]); keep the
    # flat {name: value} form too for the inline simulation spec.
    spec["variables"] = [
        {"key": key, "value": value, "is_secret": 0}
        for key, value in variables.items()
    ]
    spec["knowledge_base"] = kb_files

    normalized = factory.normalize_spec(spec)
    fields = factory.merged_fields(normalized, "sim")  # DEFAULTS + values
    return {"template_id": template.get("id", template_id),
            "spec": spec, "fields": fields,
            "system_prompt": spec["system_prompt"],
            "tools": tools, "skills": skills, "knowledge_base": kb_files,
            "variables": variables}


def build_simulation_agent_spec(template_id: str,
                                sim_id: str,
                                params: Optional[Dict[str, Any]] = None,
                                overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the row-less runtime agent dict for a template simulation.

    Shape mirrors a DB agent row (via the factory's DEFAULTS) plus the inline
    simulation spec and the containment activation keys.
    """
    from backend import agent_factory as factory

    built = build_agent_spec(template_id, params=params, overrides=overrides)
    fields = dict(built["fields"])

    agent: Dict[str, Any] = dict(fields)
    agent["id"] = sim_id
    agent["name"] = f"{fields.get('name') or built['template_id']} (simulation)"
    agent["description"] = fields.get("description") or ""
    agent["system_prompt"] = built["system_prompt"]
    agent["enabled"] = 1
    agent["is_super"] = False
    # NOTE: deliberately NOT is_subagent — own-id resolution in context._effective_id
    # and no scratchpad-workdir override.  Registry membership alone routes the
    # per-agent sidecar files to /tmp.
    agent["is_simulation"] = True
    agent["simulation_id"] = sim_id
    agent["simulation_root"] = sim_root_for(sim_id)
    agent["workspace"] = os.path.join(sim_root_for(sim_id), "workspace")
    # Force containment regardless of the template's flags (belt + suspenders;
    # the backend registry also forces this, see simulation_scope.force_sandbox).
    agent["sandbox_enabled"] = 1
    agent["workplace_id"] = None
    agent["run_as_user"] = None
    agent["_sandbox_parent_session_id"] = None
    agent["_sandbox_parent_workspace"] = None
    # Skip turn prefetch: it rebuilds its own agent_context from DB lookups that
    # are empty for a row-less id.  The main path is fully sim-aware.
    agent["disable_turn_prefetch"] = 1
    # Inline spec (see backend.agent_runtime.simulation_spec).
    agent["_simulation_tool_ids"] = factory.resolve_tools(
        built["tools"],
        artifacts_enabled=bool(fields.get("artifacts_enabled", 1)),
        vision_enabled=bool(fields.get("vision_enabled", 1)),
    )
    agent["_simulation_skill_ids"] = list(built["skills"])
    agent["_simulation_variables"] = dict(built["variables"])
    agent["_simulation_model_id"] = fields.get("model_id")
    return agent


# ---------------------------------------------------------------------------
# Materialisation
# ---------------------------------------------------------------------------

def materialize(agent: Dict[str, Any]) -> Dict[str, str]:
    """Write the sim agent's workspace + KB under its throwaway root.

    Layout mirrors the live tree so ``/<sim_root>/agents/<id>`` (KB, SYSTEM.md)
    and ``/<sim_root>/shared/agents/<id>`` (workspace, artifacts) resolve
    through the existing ``/_self/`` machinery.
    """
    sim_id = agent["id"]
    root = agent.get("simulation_root") or sim_root_for(sim_id)
    agent_dir = os.path.join(root, "agents", sim_id)
    kb_dir = os.path.join(agent_dir, "kb")
    shared_dir = os.path.join(root, "shared", "agents", sim_id)
    workspace_dir = os.path.join(shared_dir, "workspace")
    artifacts_dir = os.path.join(shared_dir, "artifacts")

    for d in (kb_dir, workspace_dir, artifacts_dir):
        os.makedirs(d, exist_ok=True)

    system_prompt = agent.get("system_prompt") or ""
    if system_prompt:
        with open(os.path.join(agent_dir, "SYSTEM.md"), "w", encoding="utf-8") as fh:
            fh.write(system_prompt)

    for item in (agent.get("_simulation_kb_files") or []):
        rel = (item or {}).get("path") or ""
        if not rel:
            continue
        target = os.path.join(kb_dir, rel)
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(item.get("content") or "")

    return {"root": root, "agent_dir": agent_dir,
            "kb_dir": kb_dir, "workspace": workspace_dir,
            "artifacts": artifacts_dir}


# ---------------------------------------------------------------------------
# Run / teardown
# ---------------------------------------------------------------------------

class SimulationSession:
    """A live, row-less simulation run with guaranteed teardown.

    Use as a context manager::

        with SimulationSession(template_id, params) as sim:
            result = sim.run("hello")
            sim.outbox   # captured outbound effects

    ``__exit__`` (or :meth:`close`) always tears the run down — on success,
    error, or if the caller abandons the generator mid-stream.
    """

    def __init__(self, template_id: str,
                 params: Optional[Dict[str, Any]] = None,
                 overrides: Optional[Dict[str, Any]] = None,
                 *,
                 sim_id: Optional[str] = None,
                 external_user_id: str = "__simulation__",
                 agent_id: Optional[str] = None,
                 orphan_ttl: float = DEFAULT_ORPHAN_TTL):
        self.template_id = template_id
        self.params = dict(params or {})
        self.overrides = dict(overrides or {})
        self.sim_id = sim_id or new_simulation_id(template_id)
        self.external_user_id = external_user_id
        self.orphan_ttl = orphan_ttl
        self.agent: Optional[Dict[str, Any]] = None
        self.session_id: Optional[str] = None
        self.outbox: List[dict] = []
        self._closed = False
        self._token = None

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> "SimulationSession":
        """Register the ephemeral agent, materialise its tree, activate scope."""
        from backend.subagent_manager import subagent_manager
        from backend.agent_runtime import llm_tool_executor as _itx

        agent = build_simulation_agent_spec(
            self.template_id, self.sim_id, params=self.params, overrides=self.overrides)
        # Carry the resolved KB so materialize() can write it without re-reading
        # the template layer.
        built = build_agent_spec(self.template_id, params=self.params, overrides=self.overrides)
        agent["_simulation_kb_files"] = built["knowledge_base"]

        subagent_manager.register_ephemeral(self.sim_id, agent)
        try:
            materialize(agent)
        except Exception:
            subagent_manager.deregister_ephemeral(self.sim_id)
            raise

        self.agent = agent
        # Canonical outbox surface for this sim (also serves the UI).
        self.outbox = _itx.get_simulated_outbox(self.sim_id)
        self.agent["simulated_outbox"] = self.outbox
        self._token = _sim_scope().set_active_simulation(
            self.sim_id, root=agent["simulation_root"])
        with _sessions_lock:
            _sessions[self.sim_id] = self
        return self

    def touch(self) -> None:
        """Refresh the run's tree mtime so the orphan sweeper leaves it alone."""
        try:
            os.utime(self.agent["simulation_root"], None)
        except OSError:
            pass

    def run(self, message: str, *, message_history: Optional[List[dict]] = None) -> Dict[str, Any]:
        """Send one user message through the real runtime and return the result."""
        from backend.agent_runtime import agent_runtime as _rt
        from models.db import db

        if self.agent is None:
            raise RuntimeError("simulation session is not open")
        self.touch()

        if message_history:
            self._seed_history(message_history)
        else:
            self._ensure_session()

        result = _rt.handle_message(
            self.sim_id, self.external_user_id, message,
            session_id=self.session_id, skip_buffer=True)
        self.outbox = list(self.agent.get("simulated_outbox") or [])
        return result

    def _ensure_session(self) -> str:
        from models.db import db
        if self.session_id is None:
            self.session_id = db.get_or_create_session(
                self.sim_id, self.external_user_id, None, db_agent_id=None)
        return self.session_id

    def _seed_history(self, history: List[dict]) -> None:
        """Persist the replay history into the sim's throwaway chat DB.

        The LAST user turn becomes the message passed to ``run`` — callers pass
        either the full history (``run(history[-1])``) or seed separately.
        """
        from models.db import db
        sid = self._ensure_session()
        for entry in history:
            role = (entry or {}).get("role") or "user"
            content = (entry or {}).get("content") or ""
            db.add_chat_message(sid, role, content, agent_id=self.sim_id)

    # -- teardown ----------------------------------------------------------

    def close(self) -> Dict[str, Any]:
        if self._closed:
            return {"simulation_id": self.sim_id, "already_closed": True}
        self._closed = True
        report = teardown(self.sim_id, session_id=self.session_id,
                          token=self._token)
        return report

    def __enter__(self) -> "SimulationSession":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self.close()
        except Exception:
            _logger.warning("simulation teardown failed for %s", self.sim_id,
                            exc_info=True)
        return False


def teardown(sim_id: str, session_id: Optional[str] = None,
             token: Any = None) -> Dict[str, Any]:
    """Tear a simulation down completely.  Never raises.

    Removes, in order: the in-memory registration, the scope contextvar, the
    sidecar-file caches (chat DB / chatlog / llm-trace), the ``session_index``
    rows the run materialised, the ``/tmp`` sub-agent tree, the simulation root
    (+ scratch + any leaked live dirs) and the captured outbox.
    """
    from backend.subagent_manager import subagent_manager
    report: Dict[str, Any] = {"simulation_id": sim_id, "removed": [], "warnings": []}

    with _sessions_lock:
        _sessions.pop(sim_id, None)

    # 1. Drop the in-memory registration first so is_subagent() flips back.
    try:
        if subagent_manager.deregister_ephemeral(sim_id):
            report["removed"].append("registry")
    except Exception as exc:
        report["warnings"].append(f"registry: {exc}")

    # 2. Release the verification contextvar (if we created it).
    try:
        if token is not None:
            _sim_scope().clear_active_simulation(token)
    except Exception as exc:
        report["warnings"].append(f"contextvar: {exc}")

    # 3. Evict cached sidecar handles so they can't hold a deleted inode.
    try:
        from models.chat import agent_chat_manager
        agent_chat_manager.drop(sim_id)
        report["removed"].append("chat-db-cache")
    except Exception as exc:
        report["warnings"].append(f"chat-db-cache: {exc}")
    try:
        from models.chatlog import chatlog_manager
        from models.llm_trace import llm_trace_manager
        if session_id:
            chatlog_manager.evict(sim_id, session_id)
            llm_trace_manager.evict(sim_id, session_id)
    except Exception as exc:
        report["warnings"].append(f"log-cache: {exc}")

    # 4. Remove the session_index rows the run created in the live DB.
    try:
        from models.db import db
        rows = db.get_sessions_by_agent(sim_id) if hasattr(db, "get_sessions_by_agent") else []
        sids = {session_id} if session_id else set()
        for row in (rows or []):
            sid = row.get("id") or row.get("session_id")
            if sid:
                sids.add(sid)
        for sid in sids:
            if sid:
                db._remove_session_index(sid)
        report["removed"].append(f"session_index ({len(sids)})")
    except Exception as exc:
        report["warnings"].append(f"session_index: {exc}")

    # 5. Remove the throwaway sub-agent sidecar tree (honour a patched root, e.g.
    #    under tests, so the /tmp routing used by chat DB / chatlog is matched).
    try:
        from models.chat import SUB_AGENTS_TMP_DIR as _sub_tmp_root
    except Exception:
        _sub_tmp_root = _SUB_AGENT_TMP
    sub_tmp = os.path.join(_sub_tmp_root, sim_id)
    if os.path.isdir(sub_tmp):
        shutil.rmtree(sub_tmp, ignore_errors=True)
        report["removed"].append(sub_tmp)

    # 6. Simulation root + scratch + leaked live dirs + backend containers.
    try:
        scope_report = _sim_scope().cleanup_simulation(sim_id)
        report["removed"].extend(scope_report.get("removed", []))
        report["warnings"].extend(scope_report.get("warnings", []))
    except Exception as exc:
        report["warnings"].append(f"cleanup_simulation: {exc}")

    # 7. Drop the captured outbox.
    try:
        from backend.agent_runtime import llm_tool_executor as _itx
        _itx.reset_simulated_outbox(sim_id)
    except Exception:
        pass

    _logger.info("Simulation torn down: %s (%d removed, %d warnings)",
                 sim_id, len(report["removed"]), len(report["warnings"]))
    return report


# ---------------------------------------------------------------------------
# Orphan sweeper
# ---------------------------------------------------------------------------

def sweep_orphans(max_age: float = DEFAULT_ORPHAN_TTL) -> List[str]:
    """Reap simulation trees + sub-agent sidecar dirs older than *max_age*.

    Only trees whose sim id is NOT currently active are removed, so a live run
    is never disturbed.  Returns the list of reaped sim ids.
    """
    reaped: List[str] = []
    if os.path.isdir(SIM_ROOT_BASE):
        try:
            entries = os.listdir(SIM_ROOT_BASE)
        except OSError:
            entries = []
        for entry in entries:
            if entry.startswith("__"):
                continue
            if is_active(entry):
                continue
            path = os.path.join(SIM_ROOT_BASE, entry)
            try:
                age = time.time() - os.path.getmtime(path)
            except OSError:
                continue
            if age < max_age:
                continue
            teardown(entry)
            reaped.append(entry)

    # Stale sub-agent sidecar dirs (e.g. from a hard crash) not owned by a live
    # registered sub-agent/sim.
    try:
        from backend.subagent_manager import subagent_manager
        if os.path.isdir(_SUB_AGENT_TMP):
            for entry in os.listdir(_SUB_AGENT_TMP):
                if subagent_manager.is_subagent(entry) or is_active(entry):
                    continue
                path = os.path.join(_SUB_AGENT_TMP, entry)
                try:
                    age = time.time() - os.path.getmtime(path)
                except OSError:
                    continue
                if age >= max_age:
                    shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass

    if reaped:
        _logger.info("Simulation orphan sweeper reaped: %s", ", ".join(reaped))
    return reaped


def start_orphan_sweeper(interval: float = DEFAULT_SWEEP_INTERVAL,
                         max_age: float = DEFAULT_ORPHAN_TTL) -> bool:
    """Start the periodic orphan sweeper (idempotent).  Returns True if started."""
    global _sweeper_thread
    with _sweeper_lock:
        if _sweeper_thread is not None and _sweeper_thread.is_alive():
            return False
        _sweeper_stop.clear()

        def _loop():
            # Reap once at start (covers a crash since the previous boot),
            # then on the interval.
            while not _sweeper_stop.wait(0):
                try:
                    sweep_orphans(max_age=max_age)
                except Exception:
                    _logger.warning("orphan sweep failed", exc_info=True)
                if _sweeper_stop.wait(interval):
                    break

        _sweeper_thread = threading.Thread(
            target=_loop, name="simulation-orphan-sweeper", daemon=True)
        _sweeper_thread.start()
    _logger.info("Simulation orphan sweeper started (interval=%ss)", interval)
    return True


def stop_orphan_sweeper() -> None:
    """Stop the periodic orphan sweeper."""
    global _sweeper_thread
    _sweeper_stop.set()
    with _sweeper_lock:
        _sweeper_thread = None


# ---------------------------------------------------------------------------
# Public programmatic entry (routes / plugins / skills call this)
# ---------------------------------------------------------------------------

def simulate(template_id: str,
             params: Optional[Dict[str, Any]] = None,
             message_history: Optional[List[dict]] = None,
             *,
             overrides: Optional[Dict[str, Any]] = None,
             agent_id: Optional[str] = None,
             external_user_id: str = "__simulation__",
             sim_id: Optional[str] = None,
             orphan_ttl: float = DEFAULT_ORPHAN_TTL) -> Dict[str, Any]:
    """Run a template in a throwaway simulation and return the turn result.

    ``message_history`` is an optional conversation replay: every entry except
    the final one is seeded into the ephemeral chat DB, and the final user entry
    is the message actually processed.  (If omitted, ``params``/template drive a
    single blank turn.)

    Returns a dict::

        {
          "simulation_id": str,
          "response": str,
          "tool_trace": [...], "timeline": [...],
          "outbox": [ {tool, args, summary, ...} ],
          "error": bool,          # present only when the run raised
        }

    Teardown is guaranteed on success, error and abandonment.  Auth / rate
    limits / output caps are the caller's (route layer) responsibility.
    """
    history = list(message_history or [])
    user_message = ""
    seed_history: List[dict] = []
    if history:
        last = history[-1]
        if isinstance(last, dict) and (last.get("role") or "user") == "user":
            user_message = last.get("content") or ""
            seed_history = history[:-1]
        else:
            seed_history = history

    session = SimulationSession(
        template_id, params=params, overrides=overrides, sim_id=sim_id,
        external_user_id=external_user_id, orphan_ttl=orphan_ttl)

    out: Dict[str, Any] = {"simulation_id": session.sim_id}
    try:
        session.open()
        if seed_history:
            session._seed_history(seed_history)
        result = session.run(user_message)
        out["response"] = result.get("response")
        out["tool_trace"] = result.get("tool_trace", [])
        out["timeline"] = result.get("timeline", [])
        if isinstance(result, dict) and result.get("error"):
            out["error"] = True
            out["error_message"] = result.get("error_message")
    except Exception as exc:
        _logger.error("simulation %s failed: %s", session.sim_id, exc, exc_info=True)
        out["error"] = True
        out["error_message"] = str(exc)
        out["response"] = None
    finally:
        try:
            session.close()
        except Exception:
            _logger.warning("simulation %s teardown failed", session.sim_id,
                            exc_info=True)
        out["outbox"] = list(session.agent.get("simulated_outbox") or []) \
            if session.agent else []
    return out
