"""
simulation_scope — containment primitives for agent-template simulation runs.

A *simulation* recreates an agent from a template inside an isolated sandbox so
its REAL tools can be exercised (fidelity) without touching the live server
state.  This module centralises the switches the backend registry, the
workspace path resolvers and the teardown helper consult so containment is
enforced in ONE place instead of scattered across every tool:

* **Simulation root** — ``/tmp/evonic_agent_template_simulation/<sim_id>``.
  Every write performed during a run must land under this tree and never under
  the authoritative ``<BASE_DIR>/agents/<id>/`` or ``<BASE_DIR>/shared/agents/<id>/``.
* **Force-sandbox** — when a simulation is active the backend registry must
  pick an isolating backend (docker/bwrap) *regardless* of the template's
  ``sandbox_enabled`` / ``run_as_user`` / ``workplace_id``.  A template can no
  longer widen its own containment by shipping ``sandbox_enabled: false`` (the
  DB stores absent as ``0``), and an SSH workplace can no longer redirect the
  run to a remote host where the temp-root workspace is meaningless.
* **Artifact registry root** — the docker/bwrap backends bind
  ``<artifacts_root>/<agent_id>/artifacts`` into the sandbox.  During a
  simulation this is pointed *inside* the simulation root so the guaranteed
  artifact leak (writing to the live registry) cannot happen.

Activation keys ride on the agent-context dict the tools already receive::

    agent['simulation_id']    -> required to activate a simulation
    agent['simulation_root']  -> optional override of the default root

A contextvar fallback (:func:`set_active_simulation`) covers code paths that do
not have the agent dict at hand (e.g. background worker threads); the agent dict
always wins when both are present.

This module intentionally imports nothing from ``backend.tools._workspace`` or
the backends at import time — those modules import *us*, so doing so would
create a cycle.  Backend cleanup is performed with lazy imports instead.
"""

from __future__ import annotations

import contextvars
import importlib
import os
import shutil

try:
    from config import BASE_DIR as _BASE_DIR
except Exception:  # pragma: no cover - config always importable in practice
    _BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))

#: Root directory under which every simulation workspace is created.
SIM_ROOT_BASE = '/tmp/evonic_agent_template_simulation'

_AGENTS_DIR = os.path.join(_BASE_DIR, 'agents')
_SHARED_AGENTS_DIR = os.path.join(_BASE_DIR, 'shared', 'agents')

_active_simulation = contextvars.ContextVar('evonic_active_simulation', default=None)


# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------

def default_root(sim_id: str) -> str:
    """Return the default simulation root for *sim_id*."""
    return os.path.join(SIM_ROOT_BASE, sim_id)


def set_active_simulation(sim_id: str, root: str | None = None):
    """Activate a simulation for the current context (thread/task).

    Returns the contextvar token so the caller can :func:`clear_active_simulation`
    with it on teardown.  Prefer passing ``agent['simulation_id']`` when the
    agent dict is available — this is the fallback for contexts that lack it.
    """
    resolved = os.path.abspath(root or default_root(sim_id))
    return _active_simulation.set({'simulation_id': sim_id, 'root': resolved})


def clear_active_simulation(token=None) -> None:
    """Deactivate the active simulation (optionally via the token from set)."""
    if token is not None:
        try:
            _active_simulation.reset(token)
            return
        except (ValueError, LookupError):
            pass
    _active_simulation.set(None)


def active_simulation() -> dict | None:
    """Return ``{'simulation_id', 'root'}`` for the active simulation, or None."""
    return _active_simulation.get()


def _resolve(agent: dict | None) -> tuple:
    """Return ``(sim_id, root)`` for *agent*, falling back to the contextvar."""
    if agent:
        sim_id = agent.get('simulation_id') or agent.get('sim_id')
        if sim_id:
            root = agent.get('simulation_root') or default_root(sim_id)
            return sim_id, os.path.abspath(root)
    ctx = _active_simulation.get()
    if ctx:
        return ctx['simulation_id'], ctx['root']
    return None, None


def simulation_id(agent: dict | None = None) -> str | None:
    """Return the simulation id active for *agent* (or the context), else None."""
    return _resolve(agent)[0]


def simulation_root(agent: dict | None = None) -> str | None:
    """Return the simulation root for *agent* (or the context), else None."""
    return _resolve(agent)[1]


def is_simulation(agent: dict | None = None) -> bool:
    """True when *agent* (or the current context) is a simulation run."""
    return _resolve(agent)[0] is not None


def force_sandbox(agent: dict | None = None) -> bool:
    """True when an isolating backend MUST be used regardless of template flags."""
    return is_simulation(agent)


# ---------------------------------------------------------------------------
# Root-aware directory helpers (simulation-aware, live fallback)
# ---------------------------------------------------------------------------

def agents_dir(agent: dict | None = None) -> str:
    """``<sim_root>/agents`` during a simulation, else ``<BASE_DIR>/agents``."""
    root = simulation_root(agent)
    return os.path.join(root, 'agents') if root else _AGENTS_DIR


def shared_agents_dir(agent: dict | None = None) -> str:
    """``<sim_root>/shared/agents`` during a simulation, else ``<BASE_DIR>/shared/agents``."""
    root = simulation_root(agent)
    return os.path.join(root, 'shared', 'agents') if root else _SHARED_AGENTS_DIR


def artifacts_root(agent: dict | None = None) -> str:
    """Root under which per-agent artifact registries live (injectable per run)."""
    return shared_agents_dir(agent)


def artifacts_dir(agent: dict | None, agent_id: str | None = None) -> str:
    """Artifact registry dir for *agent_id* (defaults to the agent's id)."""
    aid = agent_id or (agent or {}).get('id') or ''
    return os.path.join(shared_agents_dir(agent), aid, 'artifacts')


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

def _is_under_sim_base(path: str) -> bool:
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(SIM_ROOT_BASE)
    except OSError:
        return False
    return real == base or real.startswith(base + os.sep)


def cleanup_simulation(sim_id: str, root: str | None = None,
                       agent: dict | None = None) -> dict:
    """Tear down everything a simulation run may have created.

    Removes, in order: the docker container / bwrap keeper keyed by *sim_id*,
    the simulation root tree, any defensive leaked ``agents/<sim_id>`` /
    ``shared/agents/<sim_id>`` directories, and the per-agent scratch dir
    ``/tmp/evonic-<sim_id>-scratchpad``.

    Returns a report dict; never raises on individual cleanup failures.
    """
    if agent is not None and not root:
        root = agent.get('simulation_root')
    resolved = os.path.abspath(root or default_root(sim_id))
    report = {'simulation_id': sim_id, 'root': resolved,
              'removed': [], 'warnings': []}

    # 1. Backend pools (docker container / bwrap keeper), keyed by unique sim id.
    for mod_name, fn_name in (
        ('backend.tools.lib.backends.docker_backend', '_destroy_container'),
        ('backend.tools.lib.backends.bwrap_backend', '_destroy_keeper'),
    ):
        try:
            mod = importlib.import_module(mod_name)
            getattr(mod, fn_name)(sim_id)
            report['removed'].append(f'{mod_name}:{fn_name}')
        except Exception as exc:  # backend may not be installed/available
            report['warnings'].append(f'{mod_name}:{fn_name}: {exc}')

    # 2. Simulation root tree (guarded so a bad override can't nuke the live tree).
    if os.path.isdir(resolved):
        if _is_under_sim_base(resolved):
            shutil.rmtree(resolved, ignore_errors=True)
            report['removed'].append(resolved)
        else:
            report['warnings'].append(f'refusing to remove non-simulation root: {resolved}')

    # 3. Defensive: ephemeral agent dirs leaked into the live tree.
    for leaked in (os.path.join(_BASE_DIR, 'agents', sim_id),
                   os.path.join(_BASE_DIR, 'shared', 'agents', sim_id)):
        if os.path.isdir(leaked):
            shutil.rmtree(leaked, ignore_errors=True)
            report['removed'].append(leaked)
            report['warnings'].append(f'removed leaked agent dir: {leaked}')

    # 4. Scratch dir (host /tmp, keyed by agent id) — mirrors _workspace.scratch_dir.
    scratch = f'/tmp/evonic-{sim_id or "default"}-scratchpad'
    if os.path.isdir(scratch):
        shutil.rmtree(scratch, ignore_errors=True)
        report['removed'].append(scratch)

    return report
