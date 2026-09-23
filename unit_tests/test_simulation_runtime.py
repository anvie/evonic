"""Tests for the ephemeral agent-template simulation runtime (task #26).

A simulation recreates an agent from a template inside a throwaway, *row-less*
in-memory registry so its REAL tools can run (fidelity) without touching live
state.  These tests assert the containment contract:

* the ephemeral agent has NO ``agents`` row and its spec rides inline;
* context resolution (tools / skills / variables / system prompt) uses that
  inline spec, so the template's FULL tool set is present (fidelity);
* a live session leaves NO rows (``agents`` / ``session_index``) and NO files
  (sim root, ``/tmp`` sub-agent sidecar tree) after teardown -- including when
  an exception unwinds mid-run;
* the outbound interceptor captures durable/external effects instead of
  executing them, and the simulated outbox surfaces them;
* the orphan sweeper reaps stale sim roots but never touches an active run.
"""

import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.db import db
from backend.subagent_manager import subagent_manager
from backend.agent_runtime import context as _ctx
from backend.agent_runtime import llm_tool_executor as _itx
from backend.agent_runtime import simulation_runtime as simrt

# The legacy "coder" skillset is a read-only template always present in
# ``skillsets/``; using it keeps the tests hermetic without writing templates.
TEMPLATE_ID = "coder"


def _sim_id(tag="t"):
    return simrt.new_simulation_id(f"test-{tag}")


def _cleanup(sim_id):
    try:
        simrt.teardown(sim_id)
    except Exception:
        pass


@pytest.fixture
def sim_id():
    sid = _sim_id()
    try:
        yield sid
    finally:
        _cleanup(sid)


def _count_session_index(agent_id):
    with db._connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM session_index WHERE agent_id = ?", (agent_id,)
        ).fetchone()
    return row[0]


def _sub_tmp_dir(sim_id):
    from models.chat import SUB_AGENTS_TMP_DIR
    return os.path.join(SUB_AGENTS_TMP_DIR, sim_id)


# ---------------------------------------------------------------------------
# 1. Row-less ephemeral registry + inline spec
# ---------------------------------------------------------------------------

def test_ephemeral_agent_is_row_less_and_carries_inline_spec(sim_id):
    agent = simrt.build_simulation_agent_spec(TEMPLATE_ID, sim_id)

    assert db.get_agent(sim_id) is None                       # NO agents row
    assert agent["is_simulation"] is True
    assert agent["id"] == sim_id
    assert "is_subagent" not in agent                         # own-id resolution
    assert agent["simulation_id"] == sim_id
    assert agent["simulation_root"] == simrt.sim_root_for(sim_id)
    assert agent["simulation_root"].startswith(simrt.SIM_ROOT_BASE)
    # Containment flags forced regardless of the template.
    assert agent["sandbox_enabled"] == 1
    assert agent["workplace_id"] is None
    assert agent["disable_turn_prefetch"] == 1
    # Inline spec keys present (fidelity source).
    assert agent["_simulation_tool_ids"]
    assert isinstance(agent["_simulation_variables"], dict)
    assert "software developer" in agent["system_prompt"]


def test_register_and_deregister_ephemeral(sim_id):
    agent = simrt.build_simulation_agent_spec(TEMPLATE_ID, sim_id)
    subagent_manager.register_ephemeral(sim_id, agent)
    try:
        # Runtime fallback + /tmp sidecar routing key off registry membership.
        assert subagent_manager.get(sim_id) is agent
        assert subagent_manager.is_subagent(sim_id) is True
    finally:
        assert subagent_manager.deregister_ephemeral(sim_id) is True
    assert subagent_manager.is_subagent(sim_id) is False
    assert subagent_manager.get(sim_id) is None


def test_register_ephemeral_rejects_duplicate(sim_id):
    agent = simrt.build_simulation_agent_spec(TEMPLATE_ID, sim_id)
    subagent_manager.register_ephemeral(sim_id, agent)
    try:
        with pytest.raises(ValueError):
            subagent_manager.register_ephemeral(sim_id, agent)
    finally:
        subagent_manager.deregister_ephemeral(sim_id)


# ---------------------------------------------------------------------------
# 2. Full tool/skill/variable fidelity from the inline spec
# ---------------------------------------------------------------------------

def test_context_resolves_full_template_tools(sim_id):
    agent = simrt.build_simulation_agent_spec(
        TEMPLATE_ID, sim_id,
        overrides={"tools": ["read_file", "write_file", "bash"]},
    )
    # A row-less id yields NO DB tools ...
    assert db.get_agent_tools(sim_id) == []
    # ... yet the sim agent still exposes the template's tools (inline spec).
    names = {t["function"]["name"] for t in _ctx.build_tools(agent)}
    assert {"read_file", "write_file", "bash"} <= names
    assert _ctx.build_system_prompt(agent).strip() != ""


def test_inline_tool_and_skill_accessors(sim_id):
    agent = simrt.build_simulation_agent_spec(
        TEMPLATE_ID, sim_id,
        overrides={"tools": ["read_file"], "skills": []},
    )
    from backend.agent_runtime import simulation_spec as spec
    assert spec.is_simulation(agent) is True
    assert "read_file" in spec.tool_ids(agent, sim_id)
    assert spec.variables_dict(agent, sim_id) == {}
    # Non-sim passthrough untouched (empty for a row-less id).
    assert spec.tool_ids({"id": sim_id}, sim_id) == []


# ---------------------------------------------------------------------------
# 3. Outbound interceptor: captured, not executed
# ---------------------------------------------------------------------------

def _sim_agent_context(agent):
    return {
        "id": agent["id"], "agent_id": agent["id"], "name": agent["name"],
        "simulation_id": agent["simulation_id"],
        "simulation_root": agent["simulation_root"],
        "is_simulation": True,
        "sandbox_enabled": 1,
    }


def test_outbound_effects_captured_not_executed(sim_id):
    agent = simrt.build_simulation_agent_spec(TEMPLATE_ID, sim_id)
    ctx = _sim_agent_context(agent)
    executed = []

    def real_exec(fn_name, args):
        executed.append((fn_name, args))
        return {"success": True, "real": True}

    for tool, args in (
        ("create_schedule", {"name": "ping", "trigger_type": "date"}),
        ("send_agent_message", {"target_agent_id": "someone", "message": "hi"}),
    ):
        result = _execute(tool, args, real_exec, ctx)
        assert result["simulated"] is True
        assert result["success"] is True

    assert executed == []  # nothing real ran

    outbox = _itx.get_simulated_outbox(sim_id)
    tools = [e["tool"] for e in outbox]
    assert tools == ["create_schedule", "send_agent_message"]
    assert all(e["simulated"] for e in outbox)


def _execute(tool, args, real_exec, agent_context):
    from backend.agent_runtime.llm_call import _execute_tool_core
    return _execute_tool_core(tool, args, lambda *a: None, real_exec,
                              agent_context=agent_context)


def test_normal_agent_execution_unaffected():
    """No simulation marker => the interceptor is a strict no-op."""
    executed = []

    def real_exec(fn_name, args):
        executed.append(fn_name)
        return {"success": True}

    ctx = {"id": "real_agent", "agent_id": "real_agent"}
    result = _execute("create_schedule", {"name": "x"}, real_exec, ctx)
    assert executed == ["create_schedule"]
    assert "simulated" not in result


# ---------------------------------------------------------------------------
# 4. Session lifecycle: no rows, no files (incl. error unwind)
# ---------------------------------------------------------------------------

def test_session_leaves_no_rows_and_no_files(sim_id):
    session = simrt.SimulationSession(TEMPLATE_ID, sim_id=sim_id)
    session.open()
    try:
        assert os.path.isdir(simrt.sim_root_for(sim_id))
        assert session.agent is not None
        # Mimic a processed turn: a session row is materialised in the live DB.
        sid = session._ensure_session()
        assert sid
        assert _count_session_index(sim_id) >= 1
        session.session_id = sid
    finally:
        session.close()

    assert db.get_agent(sim_id) is None
    assert subagent_manager.is_subagent(sim_id) is False
    assert not os.path.isdir(simrt.sim_root_for(sim_id))
    assert not os.path.isdir(_sub_tmp_dir(sim_id))
    assert _count_session_index(sim_id) == 0


def test_teardown_runs_on_error_unwind(sim_id):
    session = simrt.SimulationSession(TEMPLATE_ID, sim_id=sim_id)
    with pytest.raises(RuntimeError):
        with session:
            sid = session._ensure_session()
            session.session_id = sid
            raise RuntimeError("boom mid-stream")

    assert not os.path.isdir(simrt.sim_root_for(sim_id))
    assert not os.path.isdir(_sub_tmp_dir(sim_id))
    assert session._closed is True
    assert _count_session_index(sim_id) == 0


def test_open_failure_deregisters(sim_id, monkeypatch):
    """If materialisation fails, the registry entry is rolled back."""
    def boom(agent):
        raise OSError("disk full")

    monkeypatch.setattr(simrt, "materialize", boom)
    session = simrt.SimulationSession(TEMPLATE_ID, sim_id=sim_id)
    with pytest.raises(OSError):
        session.open()
    assert subagent_manager.is_subagent(sim_id) is False


# ---------------------------------------------------------------------------
# 5. Orphan sweeper
# ---------------------------------------------------------------------------

def test_orphan_sweeper_reaps_stale_keeps_active(sim_id):
    stale = _sim_id("stale")
    os.makedirs(simrt.sim_root_for(stale), exist_ok=True)
    # Force an old mtime so the sweeper considers it abandoned.
    old = 1_000_000.0
    os.utime(simrt.sim_root_for(stale), (old, old))

    session = simrt.SimulationSession(TEMPLATE_ID, sim_id=sim_id)
    session.open()
    try:
        # Active run is protected regardless of age.
        os.utime(simrt.sim_root_for(sim_id), (old, old))
        reaped = simrt.sweep_orphans(max_age=60)
        assert stale in reaped
        assert sim_id not in reaped
        assert not os.path.isdir(simrt.sim_root_for(stale))
        assert os.path.isdir(simrt.sim_root_for(sim_id))
    finally:
        session.close()
        shutil.rmtree(simrt.sim_root_for(stale), ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. End-to-end simulate() with a mocked LLM: nothing real happens
# ---------------------------------------------------------------------------

def test_simulate_end_to_end_captures_outbox_no_side_effects(sim_id, monkeypatch):
    """Full lifecycle through the REAL tool chokepoint.

    Opens a live session (which materialises the workspace and writes a real
    ``session_index`` row, exactly as a run would) and drives the actual
    ``_execute_tool_core`` chokepoint with the runtime-shaped agent context.
    The outbound tools must be intercepted (captured, never executed), and
    teardown must leave no rows and no files behind.
    """
    from backend.agent_runtime import llm_call as _call

    schedules_before = _schedule_count()

    session = simrt.SimulationSession(
        TEMPLATE_ID, sim_id=sim_id, orphan_ttl=9999,
        overrides={"tools": ["read_file", "create_schedule", "send_agent_message"]})
    executed = []

    def real_exec(fn_name, args):
        executed.append((fn_name, dict(args)))
        return {"success": True}

    def builtin_exec(fn_name, args):
        return None

    try:
        session.open()
        # Materialised throwaway tree mirrors the live layout.
        assert os.path.isdir(simrt.sim_root_for(sim_id))
        assert os.path.isdir(os.path.join(
            simrt.sim_root_for(sim_id), "agents", sim_id, "kb"))

        # A live session writes a real session_index row (as run() would).
        sid = session._ensure_session()
        assert _count_session_index(sim_id) == 1

        # Runtime-shaped agent context carries the simulation activation keys,
        # so the interceptor (task #25) engages on the real chokepoint.
        ctx = dict(session.agent)
        ctx.update({"_db_agent_id": sim_id, "session_id": sid,
                    "user_id": "__simulation__"})

        r1 = _call._execute_tool_core(
            "create_schedule",
            {"name": "sim reminder", "trigger_type": "date",
             "trigger_config": {"run_date": "2999-01-01T00:00:00"},
             "action_type": "static_message", "action_config": {}},
            builtin_exec, real_exec, ctx)
        r2 = _call._execute_tool_core(
            "send_agent_message", {"target_agent_id": "other", "message": "hi"},
            builtin_exec, real_exec, ctx)

        # Intercepted -> synthetic success, real executor never invoked.
        assert r1["simulated"] is True and r2["simulated"] is True
        assert executed == []
        assert _schedule_count() == schedules_before

        # ... and both effects were captured in the simulated outbox.
        tools = [e["tool"] for e in _itx.get_simulated_outbox(sim_id)]
        assert tools == ["create_schedule", "send_agent_message"]
    finally:
        session.close()

    # Guaranteed teardown: NO rows, NO files.
    assert db.get_agent(sim_id) is None
    assert not os.path.isdir(simrt.sim_root_for(sim_id))
    assert not os.path.isdir(_sub_tmp_dir(sim_id))
    assert _count_session_index(sim_id) == 0
    assert _itx.get_simulated_outbox(sim_id) == []


def _has_tool(tools, name):
    for t in (tools or []):
        if (t.get("function") or {}).get("name") == name:
            return True
    return False


def _schedule_count():
    with db._connect() as conn:
        try:
            return conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# 7. Mid-stream disconnect (generator / SSE close) still tears down
# ---------------------------------------------------------------------------

def _sim_generator(sim_id):
    """Mimic the route layer streaming a simulation over SSE.

    Teardown lives in the generator's ``finally`` so abandoning the stream
    (``gen.close()``) still runs it.
    """
    session = simrt.SimulationSession(TEMPLATE_ID, sim_id=sim_id, orphan_ttl=9999)
    try:
        session.open()
        session._ensure_session()
        yield {"event": "chunk", "n": 1}
        yield {"event": "chunk", "n": 2}
    finally:
        session.close()


def test_teardown_on_mid_stream_disconnect(sim_id):
    gen = _sim_generator(sim_id)
    first = next(gen)
    assert first == {"event": "chunk", "n": 1}
    assert os.path.isdir(simrt.sim_root_for(sim_id))     # live while streaming
    gen.close()                                          # client disconnects

    # Nothing survives the disconnect.
    assert db.get_agent(sim_id) is None
    assert not os.path.isdir(simrt.sim_root_for(sim_id))
    assert not os.path.isdir(_sub_tmp_dir(sim_id))
    assert _count_session_index(sim_id) == 0
    assert _itx.get_simulated_outbox(sim_id) == []
