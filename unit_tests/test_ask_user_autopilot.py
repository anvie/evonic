"""ask_user must not reach an agent that is working a kanban task on autopilot.

Autopilot runs a task with nobody watching the chat, and an open question card
has no timeout, so the tool would block the run indefinitely. Core exposes a
generic tool-suppressor hook; the kanban plugin uses it to hide/refuse
``ask_user`` while a task is pending or active.
"""

import sys
import unittest.mock as mock

import pytest

import backend.plugin_hooks as plugin_hooks


@pytest.fixture(autouse=True)
def _restore_suppressors():
    """Keep every registered suppressor out of the other test modules."""
    saved = list(plugin_hooks._tool_suppressors)
    yield
    plugin_hooks._tool_suppressors[:] = saved


# ─── Registry ────────────────────────────────────────────────────────────────

def test_register_and_unregister_suppressor():
    suppressor = lambda agent_id, tool_name: None
    plugin_hooks.register_tool_suppressor(suppressor)
    assert suppressor in plugin_hooks._tool_suppressors

    plugin_hooks.register_tool_suppressor(suppressor)
    assert plugin_hooks._tool_suppressors.count(suppressor) == 1

    plugin_hooks.unregister_tool_suppressor(suppressor)
    assert suppressor not in plugin_hooks._tool_suppressors
    plugin_hooks.unregister_tool_suppressor(suppressor)  # idempotent, must not raise


def test_first_non_empty_reason_wins():
    first = lambda agent_id, tool_name: 'first reason' if tool_name == 'ask_user' else None
    second = lambda agent_id, tool_name: 'second reason'
    plugin_hooks.register_tool_suppressor(first)
    plugin_hooks.register_tool_suppressor(second)

    assert plugin_hooks.suppressed_tool_reason('a1', 'ask_user') == 'first reason'
    assert plugin_hooks.suppressed_tool_reason('a1', 'bash') == 'second reason'


def test_broken_suppressor_is_ignored_and_does_not_hide_tools():
    def broken(agent_id, tool_name):
        raise RuntimeError('suppressor crashed')

    later = lambda agent_id, tool_name: 'later reason'
    plugin_hooks.register_tool_suppressor(broken)
    assert plugin_hooks.suppressed_tool_reason('a1', 'ask_user') is None  # fails open

    plugin_hooks.register_tool_suppressor(later)
    assert plugin_hooks.suppressed_tool_reason('a1', 'ask_user') == 'later reason'


# ─── build_tools ─────────────────────────────────────────────────────────────

def _agent():
    return {
        'id': 'autopilot_agent',
        'is_super': False,
        'builtin_tools_enabled': False,
        'agent_messaging_enabled': False,
        'vision_enabled': False,
        'ask_user_enabled': 1,
    }


def _isolate_tool_registry(monkeypatch):
    """Only advertise ask_user, so the assertion is about the suppression."""
    from backend.agent_runtime import context

    tool_def = {
        'id': 'ask_user',
        'function': {'name': 'ask_user', 'description': 'd', 'parameters': {'type': 'object'}},
    }
    monkeypatch.setattr(context.db, 'get_agent_tools', lambda agent_id: [])
    monkeypatch.setattr(context.db, 'get_agent_skills', lambda agent_id: [])
    monkeypatch.setattr(context.tool_registry, 'get_all_tool_defs', lambda: [tool_def])


def _tool_names(agent):
    from backend.agent_runtime.context import build_tools
    return {t['function']['name'] for t in build_tools(agent) if t.get('function', {}).get('name')}


def test_build_tools_hides_ask_user_when_a_suppressor_answers(monkeypatch):
    _isolate_tool_registry(monkeypatch)
    plugin_hooks.register_tool_suppressor(
        lambda agent_id, tool_name: 'autopilot is busy' if tool_name == 'ask_user' else None
    )

    assert 'ask_user' not in _tool_names(_agent())


def test_build_tools_keeps_ask_user_without_a_suppressor(monkeypatch):
    _isolate_tool_registry(monkeypatch)

    assert 'ask_user' in _tool_names(_agent())


# ─── ask_user.execute ────────────────────────────────────────────────────────

def test_execute_refuses_without_creating_a_pending_question(monkeypatch):
    from backend.ask_user_store import question_registry
    from backend.tools import ask_user as ask_user_tool

    monkeypatch.setattr(ask_user_tool, '_enabled_for', lambda agent: True)
    monkeypatch.setattr(
        'backend.plugin_manager.suppressed_tool_reason',
        lambda agent_id, tool_name: 'autopilot is working on kanban task #12',
    )
    with mock.patch.object(question_registry, 'create') as create:
        result = ask_user_tool.execute(
            {'id': 'autopilot_agent', 'session_id': 's1', 'user_id': 'web_test'},
            {'questions': [{'question': 'Which one?', 'header': 'Pick',
                            'options': [{'label': 'A'}, {'label': 'B'}]}]},
        )

    assert result['status'] == 'unavailable'
    assert 'autopilot is working on kanban task #12' in result['error']
    assert 'do not call ask_user again' in result['error']
    create.assert_not_called()


def test_execute_still_asks_when_nothing_is_suppressed(monkeypatch):
    from backend.tools import ask_user as ask_user_tool

    monkeypatch.setattr(ask_user_tool, '_enabled_for', lambda agent: True)
    monkeypatch.setattr('backend.plugin_manager.suppressed_tool_reason',
                        lambda agent_id, tool_name: None)
    # With no questions the tool stops at argument validation, which proves it
    # was not short-circuited by the suppressor.
    result = ask_user_tool.execute({'id': 'a1', 'user_id': 'web_test'}, {})

    assert 'error' in result
    assert 'Decide using your best judgement' not in result['error']


# ─── kanban suppressor ───────────────────────────────────────────────────────

def _kanban_handler():
    if 'plugins.kanban.handler' in sys.modules:
        return sys.modules['plugins.kanban.handler']
    import plugins.kanban.handler as handler
    return handler


@pytest.fixture
def kanban():
    handler = _kanban_handler()
    handler._active_tasks.clear()
    handler._pending_tasks.clear()
    yield handler
    handler._active_tasks.clear()
    handler._pending_tasks.clear()


def _suppress(monkeypatch, handler, agent_id, tool_name='ask_user', autopilot=True,
              active=None, pending=None, db_task=None):
    monkeypatch.setattr(handler, '_is_autopilot', lambda aid: autopilot)
    if active:
        handler._active_tasks[agent_id] = active
    if pending:
        handler._pending_tasks[agent_id] = pending
    with mock.patch('plugins.kanban.db.kanban_db.get_active_task_for_agent',
                    return_value=db_task):
        return handler._ask_user_suppressor(agent_id, tool_name)


def test_kanban_allows_ask_user_when_autopilot_is_off(kanban, monkeypatch):
    result = _suppress(monkeypatch, kanban, 'siwa', autopilot=False, active='12')

    assert result is None


def test_kanban_suppresses_ask_user_while_a_task_is_active(kanban, monkeypatch):
    reason = _suppress(monkeypatch, kanban, 'siwa', active='12')

    assert reason is not None
    assert '#12' in reason
    assert 'autopilot' in reason


def test_kanban_suppresses_ask_user_while_a_task_is_pending(kanban, monkeypatch):
    reason = _suppress(monkeypatch, kanban, 'siwa', pending='7')

    assert reason is not None and '#7' in reason


def test_kanban_suppresses_ask_user_after_a_restart_via_the_board(kanban, monkeypatch):
    """The in-memory maps are empty after a restart; the durable row still counts."""
    reason = _suppress(monkeypatch, kanban, 'siwa', db_task={'id': 31, 'status': 'in-progress'})

    assert reason is not None and '#31' in reason


def test_kanban_allows_ask_user_when_no_task_is_being_worked(kanban, monkeypatch):
    assert _suppress(monkeypatch, kanban, 'siwa') is None


def test_kanban_ignores_other_tools(kanban, monkeypatch):
    assert _suppress(monkeypatch, kanban, 'siwa', tool_name='bash', active='12') is None
