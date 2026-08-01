"""Regression guards: ATG additions must not change behavior when unused."""

import json

from backend.agent_state import AgentState
from backend.agent_runtime.atg import is_atg_eligible


# ── AgentState compatibility ─────────────────────────────────────────────────

def test_default_state_has_no_atg():
    assert AgentState().atg is None


def test_old_format_deserializes_without_atg_key():
    # Pre-ATG session_state blobs have no "atg" key
    old = json.dumps({"mode": "execute", "tasks": [], "next_task_id": 1,
                      "plan_file": "plan/x.md", "states": {},
                      "focus": False, "focus_reason": None,
                      "auto_trivial": True})
    ms = AgentState.deserialize(old)
    assert ms.atg is None
    assert ms.mode == "execute"
    assert ms.auto_trivial is True


def test_round_trip_preserves_atg():
    ms = AgentState()
    ms.atg = {"status": "compiled",
              "dag": {"nodes": {"n1": {"status": "pending"}}},
              "history": {"entries": []}, "repair_attempts": 0, "stats": {}}
    restored = AgentState.deserialize(ms.serialize())
    assert restored.atg == ms.atg


def test_render_without_atg_has_no_atg_section():
    assert "Atomic Task Graph" not in AgentState().render()


def test_plan_mode_instruction_is_atg_aware():
    ms = AgentState()  # plan mode
    plain = ms.render()
    assert "MUST call save_plan()" in plain
    assert "compile_task_graph" not in plain

    atg = ms.render(atg_enabled=True)
    assert "MUST call compile_task_graph" in atg
    # save_plan stays available as the explicit fallback, never the command
    assert "MUST call save_plan()" not in atg


def test_render_with_atg_is_compact():
    ms = AgentState()
    ms.atg = {"status": "executing",
              "dag": {"nodes": {"n1": {"status": "done"},
                                "n2": {"status": "done"},
                                "n3": {"status": "pending"}}}}
    rendered = ms.render()
    assert "### Atomic Task Graph" in rendered
    assert "executing" in rendered
    assert "3 nodes" in rendered
    # Never inject the raw graph JSON into the LLM context
    assert '"nodes"' not in rendered


def test_persist_split_carries_atg(tmp_path):
    from backend.agent_runtime.llm_loop import _persist_agent_state_split
    from models.db import db

    db.create_agent({'id': 'atg_test_agent', 'name': 'A', 'system_prompt': ''})
    ms = AgentState()
    ms.atg = {"status": "compiled", "dag": {"nodes": {}}}
    _persist_agent_state_split(ms, 'atg_test_agent', 'sess-1')

    raw = db.get_session_state('sess-1', agent_id='atg_test_agent')
    data = json.loads(raw)
    assert data['atg'] == ms.atg


def test_completed_atg_nodes_advance_only_unique_matching_tasks():
    ms = AgentState(tasks=[
        {'id': 1, 'text': 'Inspect configuration', 'status': 'in_progress',
         'in_progress_since': 10.0},
        {'id': 2, 'text': 'Unrelated user task', 'status': 'pending'},
        {'id': 3, 'text': 'Duplicate task', 'status': 'pending'},
        {'id': 4, 'text': 'Duplicate task', 'status': 'pending'},
    ])
    ms.atg = {'dag': {'nodes': {
        'n1': {'goal': 'inspect configuration', 'status': 'done'},
        'n2': {'goal': 'Unrelated graph goal', 'status': 'done'},
        'n3': {'goal': 'Duplicate task', 'status': 'done'},
        'n4': {'goal': 'Pending graph work', 'status': 'pending'},
    }}}

    assert ms.sync_completed_atg_tasks() == [1]
    assert [(task['id'], task['status']) for task in ms.tasks] == [
        (1, 'done'), (2, 'pending'), (3, 'pending'), (4, 'pending'),
    ]
    assert 'in_progress_since' not in ms.tasks[0]


# ── Tool exposure gate ───────────────────────────────────────────────────────

def test_compile_task_graph_hidden_without_flag():
    from backend.agent_runtime.context import build_tools
    agent = {'id': 'a1', 'is_super': False, 'builtin_tools_enabled': True}
    names = {t['function']['name'] for t in build_tools(agent)
             if t.get('function', {}).get('name')}
    assert 'compile_task_graph' not in names

    # enable_atg without enable_agent_state stays hidden too
    agent['enable_atg'] = 1
    names = {t['function']['name'] for t in build_tools(agent)
             if t.get('function', {}).get('name')}
    assert 'compile_task_graph' not in names


def test_compile_task_graph_exposed_when_flagged():
    from backend.agent_runtime.context import build_tools
    agent = {'id': 'a1', 'is_super': False, 'builtin_tools_enabled': True,
             'enable_atg': 1, 'enable_agent_state': 1}
    names = {t['function']['name'] for t in build_tools(agent)
             if t.get('function', {}).get('name')}
    assert 'compile_task_graph' in names


def test_builtin_executor_hides_compile_task_graph_without_flag():
    from backend.tools.registry import ToolRegistry
    executor = ToolRegistry().get_builtin_executor({'id': 'a1'})
    assert executor('compile_task_graph', {'goal': 'do something'}) is None


def test_builtin_executor_exposes_compile_task_graph_when_flagged():
    from backend.tools.registry import ToolRegistry
    executor = ToolRegistry().get_builtin_executor({'id': 'a1', 'enable_atg': True})
    result = executor('compile_task_graph', {'goal': 'do something'})
    assert result is not None
    assert 'error' in result


def test_always_execute_hides_plan_workflow_builtins_everywhere():
    from backend.agent_runtime.context import build_tools
    from backend.tools.registry import ToolRegistry

    hidden = {'save_plan', 'set_mode', 'state'}
    agent = {
        'id': 'always_execute_agent',
        'is_super': False,
        'builtin_tools_enabled': True,
        'always_execute': True,
    }
    context = {'id': agent['id'], 'always_execute': True}

    ui_names = {tool['name'] for tool in ToolRegistry().get_builtin_tool_defs(context)}
    runtime_names = {
        tool['function']['name'] for tool in ToolRegistry().get_builtin_tools(context)
    }
    built_names = {
        tool['function']['name'] for tool in build_tools(agent)
        if tool.get('function', {}).get('name')
    }
    executor = ToolRegistry().get_builtin_executor(context)

    assert hidden.isdisjoint(ui_names)
    assert hidden.isdisjoint(runtime_names)
    assert hidden.isdisjoint(built_names)
    assert all(executor(tool_name, {}) is None for tool_name in hidden)


def test_muktamar_registrasi_hides_disabled_atg_and_cmp_builtins():
    from backend.tools.registry import ToolRegistry

    disabled = {
        'compile_task_graph', 'forget_memory', 'new_path', 'read_transcript',
    }
    context = {
        'id': 'muktamar_registrasi',
        'enable_atg': False,
        'enable_cmp': False,
    }

    ui_names = {tool['name'] for tool in ToolRegistry().get_builtin_tool_defs(context)}
    runtime_names = {
        tool['function']['name'] for tool in ToolRegistry().get_builtin_tools(context)
    }
    assert disabled.isdisjoint(ui_names)
    assert disabled.isdisjoint(runtime_names)


def test_compile_executor_defends_when_disabled():
    from backend.tools.registry import _builtin_compile_task_graph_factory
    _, executor = _builtin_compile_task_graph_factory({'id': 'a1'})
    assert 'error' in executor({'goal': 'do something'})

    _, executor = _builtin_compile_task_graph_factory(
        {'id': 'a1', 'enable_atg': True})  # no agent_state
    assert 'error' in executor({'goal': 'do something'})

    _, executor = _builtin_compile_task_graph_factory(
        {'id': 'a1', 'enable_atg': True, 'agent_state': AgentState()})  # no runtime
    assert 'error' in executor({'goal': 'do something'})


# ── save_plan ATG enforcement ────────────────────────────────────────────────
# Probe with an empty filename: passing the ATG gate yields the filename
# validation error instead of the redirect (no file I/O in tests).

def _save_plan_exec(ctx):
    from backend.tools.registry import _builtin_save_plan_factory
    return _builtin_save_plan_factory(ctx)[1]


def test_save_plan_redirects_to_compile_when_atg_applies():
    ms = AgentState()  # plan mode, complex (auto_trivial False), no atg
    result = _save_plan_exec({'id': 'a1', 'enable_atg': True,
                              'agent_state': ms})({'filename': 'x.md',
                                                   'content': '#'})
    assert 'compile_task_graph' in result['error']


def test_save_plan_allowed_for_trivial_task():
    ms = AgentState(mode='execute', auto_trivial=True)
    result = _save_plan_exec({'id': 'a1', 'enable_atg': True,
                              'agent_state': ms})({'filename': '', 'content': ''})
    assert 'filename' in result['error']  # passed the ATG gate


def test_save_plan_allowed_after_failed_compilation():
    ms = AgentState()
    ms.atg = {'status': 'failed', 'error': 'boom'}
    result = _save_plan_exec({'id': 'a1', 'enable_atg': True,
                              'agent_state': ms})({'filename': '', 'content': ''})
    assert 'filename' in result['error']


def test_save_plan_unaffected_without_flag():
    result = _save_plan_exec({'id': 'a1',
                              'agent_state': AgentState()})({'filename': '',
                                                             'content': ''})
    assert 'filename' in result['error']


# ── Eligibility gate ─────────────────────────────────────────────────────────

def test_not_eligible_without_flag():
    assert not is_atg_eligible({}, AgentState())
    assert not is_atg_eligible({'enable_atg': 0}, AgentState())
    assert not is_atg_eligible(None, AgentState())


def test_not_eligible_without_agent_state():
    assert not is_atg_eligible({'enable_atg': 1}, None)


def test_not_eligible_for_trivial_task():
    ms = AgentState(mode="execute", auto_trivial=True)
    assert not is_atg_eligible({'enable_atg': 1}, ms)


def test_eligible_for_flagged_complex_task():
    assert is_atg_eligible({'enable_atg': 1}, AgentState())
