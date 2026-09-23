"""Outbound side-effect interceptor tests for agent-template simulations (task #25).

A simulation recreates a template's agent with its FULL tool set so the model's
reasoning stays faithful.  Workspace-scoped effects are contained by workspace
redirection (task #24); the *outbound* effects — schedules, messages, memory
writes, kanban mutations, remote calls — cannot be contained that way, so they
are intercepted at the single tool-execution chokepoint
(``llm_call._execute_tool_core``) and replaced with a synthetic success while
the call is captured into a "simulated outbox".

These tests assert:

* the denylist/allowlist classification;
* a simulated run of a template that declares ``create_schedule`` + a messaging
  tool produces **no real schedule** and **no real outbound message**, yet does
  produce simulated-outbox entries;
* normal (non-simulation) execution is unaffected — the real tool runs.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.agent_runtime import llm_tool_executor as itx
from backend.agent_runtime.llm_call import _execute_tool_core
from models.db import db

# Record of "real" outbound deliveries the harness performs.  Must stay empty
# for every simulated call.
_SENT: list = []


# ---------------------------------------------------------------------------
# Harness: a real executor that performs genuine durable/outbound effects.
# Used both to prove those effects happen when NOT simulated and to prove they
# are skipped when simulated.
# ---------------------------------------------------------------------------

def _builtin_exec(fn_name, args):
    """No builtin handles our denylisted tools."""
    return None


def _make_real_exec(owner_id):
    def real_exec(fn_name, args):
        if fn_name == "create_schedule":
            sid = args.get("schedule_id") or f"sched-{uuid.uuid4().hex[:8]}"
            db.create_schedule(
                schedule_id=sid,
                name=args.get("name", "sim-test"),
                owner_type="agent",
                owner_id=owner_id,
                trigger_type="date",
                trigger_config={"run_date": "2099-01-01T00:00:00"},
                action_type="static_message",
                action_config={"agent_id": owner_id, "message": "later"},
            )
            return db.get_schedule(sid)
        if fn_name == "send_agent_message":
            _SENT.append(dict(args))
            return {"success": True, "message": "Message sent to agent (real)."}
        return {"result": f"{fn_name} ran"}
    return real_exec


def _live_schedules(owner_id):
    return db.get_schedules(owner_type="agent", owner_id=owner_id)


@pytest.fixture(autouse=True)
def _clean_state():
    _SENT.clear()
    itx.reset_simulated_outbox()
    yield
    _SENT.clear()
    itx.reset_simulated_outbox()


@pytest.fixture
def sim_id():
    return f"testsim-{uuid.uuid4().hex[:10]}"


def _sim_context(sid, **extra):
    ctx = {"id": "agent-x", "session_id": sid, "simulation_id": sid}
    ctx.update(extra)
    return ctx


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    # durable DELAYED + irreversible
    ("create_schedule", True), ("update_schedule", True), ("cancel_schedule", True),
    # outbound messaging / delivery
    ("send_agent_message", True), ("send_channel_message", True),
    ("send_notification", True), ("escalate_to_user", True), ("send_file", True),
    # durable memory writes
    ("remember", True), ("evomem", True), ("forget_memory", True),
    # kanban mutations (namespace)
    ("kanban_create_task", True), ("kanban_update_status", True),
    ("kanban_update_task", True), ("kanban_add_comment", True),
    # remote / external
    ("database_query", True), ("sshc", True),
    # read-only members stay allowed (fidelity)
    ("kanban_search_tasks", False), ("kanban_get_task", False),
    ("kanban_get_comments", False), ("recall", False),
    # workspace-scoped / unrelated tools are never intercepted here
    ("read_file", False), ("bash", False), ("runpy", False),
    ("save_artifact", False), ("write_file", False), ("create_booking", False),
    ("", False), (None, False),
])
def test_is_outbound_side_effect(name, expected):
    assert itx.is_outbound_side_effect(name) is expected


# ---------------------------------------------------------------------------
# Normal (non-simulation) execution is unaffected
# ---------------------------------------------------------------------------

def test_normal_execution_is_never_intercepted():
    owner = f"owner-{uuid.uuid4().hex[:8]}"
    real = _make_real_exec(owner)

    r_sched = _execute_tool_core("create_schedule", {"name": "real"},
                                 _builtin_exec, real, {"id": "agent-1"})
    r_msg = _execute_tool_core(
        "send_agent_message",
        {"target_agent_id": "other", "message": "hi"},
        _builtin_exec, real, {"id": "agent-1"})

    # Real effects happened.
    assert r_sched.get("simulated") is not True
    assert db.get_schedule(r_sched["id"]) is not None
    assert len(_live_schedules(owner)) == 1
    assert len(_SENT) == 1
    assert r_msg.get("simulated") is not True

    # Nothing captured in any simulated outbox.
    assert itx.get_simulated_outbox() == []

    for s in _live_schedules(owner):
        db.delete_schedule(s["id"])


def test_missing_agent_context_is_unaffected():
    owner = f"owner-{uuid.uuid4().hex[:8]}"
    real = _make_real_exec(owner)
    # No agent_context at all (defaults to None) and no active simulation.
    r = _execute_tool_core("create_schedule", {"name": "real"}, _builtin_exec, real)
    assert r.get("simulated") is not True
    assert len(_live_schedules(owner)) == 1
    for s in _live_schedules(owner):
        db.delete_schedule(s["id"])


# ---------------------------------------------------------------------------
# Simulated run: no real effects, but simulated-outbox entries
# ---------------------------------------------------------------------------

def test_simulation_blocks_real_effects_and_records_outbox(sim_id):
    owner = f"simowner-{uuid.uuid4().hex[:8]}"
    real = _make_real_exec(owner)
    ctx = _sim_context(sim_id)

    r_sched = _execute_tool_core("create_schedule", {"name": "sim sched"},
                                 _builtin_exec, real, ctx)
    r_msg = _execute_tool_core(
        "send_agent_message",
        {"target_agent_id": "other", "message": "hi"},
        _builtin_exec, real, ctx)

    # Synthetic successes shaped like the real tools.
    assert r_sched["success"] is True and r_sched["simulated"] is True
    assert "simulated" in r_sched["message"].lower()
    assert r_sched.get("schedule_id") == r_sched["outbox_id"]
    assert r_msg["success"] is True and r_msg["simulated"] is True
    assert "simulated" in r_msg["message"].lower()

    # NO real schedule was persisted.
    assert _live_schedules(owner) == []
    # NO real outbound message was delivered.
    assert _SENT == []

    # Both calls captured in the simulated outbox.
    outbox = itx.get_simulated_outbox(sim_id)
    assert [e["tool"] for e in outbox] == ["create_schedule", "send_agent_message"]
    assert all(e["simulated"] is True for e in outbox)
    assert all(e["simulation_id"] == sim_id for e in outbox)
    assert all(e["outbox_id"] for e in outbox)


def test_allowlisted_readonly_tools_pass_through_in_simulation(sim_id):
    ctx = _sim_context(sim_id)
    ran = []

    def real_exec(fn_name, args):
        ran.append(fn_name)
        return {"result": "ok"}

    _execute_tool_core("kanban_search_tasks", {}, _builtin_exec, real_exec, ctx)
    _execute_tool_core("kanban_get_task", {"task_id": "1"}, _builtin_exec, real_exec, ctx)
    _execute_tool_core("recall", {"query": "x"}, _builtin_exec, real_exec, ctx)

    assert ran == ["kanban_search_tasks", "kanban_get_task", "recall"]
    assert itx.get_simulated_outbox(sim_id) == []


def test_kanban_mutation_is_intercepted_in_simulation(sim_id):
    ctx = _sim_context(sim_id)
    ran = []

    def real_exec(fn_name, args):
        ran.append(fn_name)
        return {"status": "success"}

    r = _execute_tool_core("kanban_update_status",
                           {"task_id": "1", "status": "done"},
                           _builtin_exec, real_exec, ctx)

    assert ran == []                      # real tool never executed
    assert r["simulated"] is True
    assert [e["tool"] for e in itx.get_simulated_outbox(sim_id)] == ["kanban_update_status"]


# ---------------------------------------------------------------------------
# Collector hooks (for the sim runtime, T5)
# ---------------------------------------------------------------------------

def test_agent_context_collector_hook(sim_id):
    collected = []
    ctx = _sim_context(sim_id, **{itx.SIMULATED_OUTBOX_KEY: collected})
    real = _make_real_exec("o")

    _execute_tool_core("create_schedule", {"name": "n"}, _builtin_exec, real, ctx)

    assert [e["tool"] for e in collected] == ["create_schedule"]
    # Canonical in-memory surface is still populated.
    assert len(itx.get_simulated_outbox(sim_id)) == 1


def test_contextvar_collector_hook(sim_id):
    collected = []
    token = itx.set_simulated_outbox(collected.append, sim_id=sim_id)
    try:
        real = _make_real_exec("o")
        _execute_tool_core("send_notification", {"message": "x"},
                           _builtin_exec, real, None)
        assert [e["tool"] for e in collected] == ["send_notification"]
    finally:
        itx.clear_simulated_outbox(token)


def test_simulation_scope_contextvar_activates_interception(sim_id):
    """Interception tracks the simulation_scope contextvar when no agent dict."""
    from backend.tools.lib import simulation_scope as sim

    token = sim.set_active_simulation(sim_id)
    try:
        ran = []

        def real_exec(fn_name, args):
            ran.append(fn_name)
            return {"result": "ok"}

        r = _execute_tool_core("sshc", {"action": "open", "host": "h"},
                               _builtin_exec, real_exec)
        assert ran == []
        assert r["simulated"] is True
        assert [e["tool"] for e in itx.get_simulated_outbox(sim_id)] == ["sshc"]
    finally:
        sim.clear_active_simulation(token)
