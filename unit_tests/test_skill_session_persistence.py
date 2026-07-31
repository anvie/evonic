"""
Unit tests for lazy skill state persistence across turns in AgentRuntime.

Covers:
  - use_skill populates _session_skill_mds / _session_skill_tools
  - On the next turn, persisted tools are injected into the tool list
  - On the next turn, persisted SYSTEM.md is injected as a system message
  - unload_skill removes skill from session state
  - clear_session wipes skill state for that session
  - No injection when session state is empty
"""

import json
import os
import sys
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ─── Constants ───────────────────────────────────────────────────────────────

SKILL_ID = 'test_skill'
SKILL_SYSTEM_MD = '# Test Skill\nFollow this workflow carefully.'
SKILL_TOOLS = [
    {'type': 'function', 'function': {
        'name': 'skill_tool_a',
        'parameters': {'type': 'object', 'properties': {}, 'required': []},
    }},
]

SESSION_ID = 'sess-test-001'
AGENT_ID = 'agent-test-001'

# ─── LLM response helpers ─────────────────────────────────────────────────────

def _llm_text(content='done'):
    return {
        'success': True,
        'response': {'choices': [{'message': {'content': content}}]},
    }


def _llm_tool_call(fn_name, args=None, cid='c1', content=''):
    return {
        'success': True,
        'response': {'choices': [{'message': {
            'content': content,
            'tool_calls': [{'id': cid, 'type': 'function',
                            'function': {'name': fn_name,
                                         'arguments': json.dumps(args or {})}}],
        }}]},
    }


# ─── Executor helpers ─────────────────────────────────────────────────────────

def _use_skill_executor():
    """Returns an executor that handles use_skill → injects SKILL_TOOLS + SKILL_SYSTEM_MD."""
    def executor(fn_name, args):
        if fn_name == 'use_skill':
            return {
                'id': SKILL_ID,
                'inject_tools': list(SKILL_TOOLS),  # fresh list — pop'd by runtime
                'system_md': SKILL_SYSTEM_MD,
                'status': 'loaded',
                'message': f'Skill {SKILL_ID} loaded',
            }
        return None
    return executor


def _unload_skill_executor():
    """Returns an executor that handles unload_skill."""
    def executor(fn_name, args):
        if fn_name == 'unload_skill':
            return {
                'id': SKILL_ID,
                'remove_tools': True,
                'message': f'Skill {SKILL_ID} unloaded',
            }
        return None
    return executor


# ─── Fixture ─────────────────────────────────────────────────────────────────

@pytest.fixture
def rt():
    """AgentRuntime with all external I/O mocked out.

    Patches are kept alive for the duration of the test via the yield.
    """
    import sys
    # test_tool_guard.py and test_llm_loop_recovery.py stub backend.agent_runtime
    # with a bare module (or one with a mock AgentRuntime).  Remove the stub and
    # all its cached submodules so the real package can be imported cleanly.
    if 'backend.agent_runtime' in sys.modules:
        ar_mod = sys.modules['backend.agent_runtime']
        ar_class = getattr(ar_mod, 'AgentRuntime', None)
        if not (ar_class is not None and isinstance(ar_class, type)):
            # Remove stub + all backend.agent_runtime.* submodules.  If submodules
            # stay in sys.modules, Python skips the "set parent attribute" step on
            # re-import, causing AttributeError when patch() traverses the dotted path.
            to_remove = [k for k in sys.modules
                         if k == 'backend.agent_runtime'
                         or k.startswith('backend.agent_runtime.')]
            for k in to_remove:
                del sys.modules[k]
            # Also reset the parent package attribute so that getattr(backend,
            # 'agent_runtime') returns the real module after re-import, not the stub.
            import backend as _bpkg
            if getattr(_bpkg, 'agent_runtime', None) is ar_mod:
                try:
                    delattr(_bpkg, 'agent_runtime')
                except AttributeError:
                    pass

    with patch('backend.agent_runtime.llm_loop.db') as _db, \
         patch('backend.agent_runtime.llm_loop.llm_client') as _llm, \
         patch('backend.agent_runtime.llm_loop.tool_registry') as _tr, \
         patch('backend.agent_runtime.context.SkillsManager'), \
         patch('backend.event_stream.event_stream'), \
         patch('backend.plugin_manager.check_tool_guards', return_value=None), \
         patch('backend.plugin_manager.run_message_interceptors', return_value=[]):

        _db.get_setting.side_effect = lambda key, default=None: default or '0'
        _db.add_chat_message.return_value = None
        _db.get_agent_model.return_value = None  # no agent model → use mocked llm_client
        _db.get_agent_default_model.return_value = None  # no custom model → use llm_client mock
        _tr.get_builtin_tools.return_value = []
        _tr.get_all_tool_defs.return_value = []
        _tr.get_builtin_executor.return_value = lambda fn, args: None
        _tr.get_real_executor.return_value = lambda fn, args: None
        # Prevent TypeError when llm_client.thinking_budget is compared with int
        _llm.thinking_budget = 0
        _llm.thinking = False

        from backend.agent_runtime import AgentRuntime
        runtime = AgentRuntime()
        runtime._mock_llm = _llm
        runtime._mock_tr = _tr
        yield runtime


# ─── Helper: run one turn ────────────────────────────────────────────────────

def _run_turn(rt, llm_responses, executor=None, session_id=SESSION_ID):
    """Drive one call to _run_tool_loop with controlled LLM responses."""
    rt._mock_llm.chat_completion.side_effect = llm_responses
    if executor is not None:
        rt._mock_tr.get_builtin_executor.return_value = executor
    agent = {'id': AGENT_ID}
    ctx = {
        'id': AGENT_ID, 'user_id': 'user-1', 'channel_id': None,
        'session_id': session_id, 'assigned_tool_ids': [], 'is_super': False,
    }
    return rt._run_tool_loop(agent, ctx, [{'role': 'user', 'content': 'hi'}], [], session_id)


# ─── Tests ───────────────────────────────────────────────────────────────────

class TestUseSkillWritesSessionState:
    """use_skill → session dicts are populated."""

    def test_system_md_stored_in_session(self, rt):
        _run_turn(rt, [_llm_tool_call('use_skill'), _llm_text()],
                  executor=_use_skill_executor())

        assert rt._session_skill_mds.get(SESSION_ID, {}).get(SKILL_ID) == SKILL_SYSTEM_MD

    def test_tool_defs_stored_in_session(self, rt):
        _run_turn(rt, [_llm_tool_call('use_skill'), _llm_text()],
                  executor=_use_skill_executor())

        stored = rt._session_skill_tools.get(SESSION_ID, {}).get(SKILL_ID, [])
        stored_names = [t.get('function', {}).get('name') for t in stored]
        assert 'skill_tool_a' in stored_names

    def test_second_use_skill_updates_stored_md(self, rt):
        """Re-loading a skill updates the stored SYSTEM.md."""
        rt._session_skill_mds[SESSION_ID] = {SKILL_ID: 'old content'}
        _run_turn(rt, [_llm_tool_call('use_skill'), _llm_text()],
                  executor=_use_skill_executor())

        assert rt._session_skill_mds[SESSION_ID][SKILL_ID] == SKILL_SYSTEM_MD


class TestSkillRestorationNextTurn:
    """Pre-populated session state → tools and SYSTEM.md appear in next turn."""

    def test_skill_tools_added_to_tool_list(self, rt):
        rt._session_skill_tools[SESSION_ID] = {SKILL_ID: list(SKILL_TOOLS)}

        captured_tools = []

        def capture_and_respond(*args, **kwargs):
            captured_tools.extend(kwargs.get('tools') or [])
            return _llm_text()

        rt._mock_llm.chat_completion.side_effect = capture_and_respond
        agent = {'id': AGENT_ID}
        ctx = {'id': AGENT_ID, 'user_id': 'u', 'channel_id': None,
               'session_id': SESSION_ID, 'assigned_tool_ids': [], 'is_super': False}
        rt._run_tool_loop(agent, ctx, [{'role': 'user', 'content': 'hi'}], [], SESSION_ID)

        tool_names = [t.get('function', {}).get('name') for t in captured_tools]
        assert 'skill_tool_a' in tool_names

    def test_system_md_injected_as_system_message(self, rt):
        rt._session_skill_mds[SESSION_ID] = {SKILL_ID: SKILL_SYSTEM_MD}

        captured_messages = []

        def capture_and_respond(*args, **kwargs):
            captured_messages.extend(kwargs.get('messages') or [])
            return _llm_text()

        rt._mock_llm.chat_completion.side_effect = capture_and_respond
        agent = {'id': AGENT_ID}
        ctx = {'id': AGENT_ID, 'user_id': 'u', 'channel_id': None,
               'session_id': SESSION_ID, 'assigned_tool_ids': [], 'is_super': False}
        rt._run_tool_loop(agent, ctx, [{'role': 'user', 'content': 'hi'}], [], SESSION_ID)

        system_contents = [m['content'] for m in captured_messages if m.get('role') == 'system']
        skill_context_msg = next(
            (c for c in system_contents if f'Skill Context: {SKILL_ID}' in c), None
        )
        assert skill_context_msg is not None
        assert SKILL_SYSTEM_MD in skill_context_msg

    def test_no_injection_without_session_state(self, rt):
        """Empty session state → no extra tools added."""
        captured_tools = []

        def capture_and_respond(*args, **kwargs):
            captured_tools.extend(kwargs.get('tools') or [])
            return _llm_text()

        rt._mock_llm.chat_completion.side_effect = capture_and_respond
        agent = {'id': AGENT_ID}
        ctx = {'id': AGENT_ID, 'user_id': 'u', 'channel_id': None,
               'session_id': SESSION_ID, 'assigned_tool_ids': [], 'is_super': False}
        rt._run_tool_loop(agent, ctx, [{'role': 'user', 'content': 'hi'}], [], SESSION_ID)

        assert captured_tools == []

    def test_different_sessions_isolated(self, rt):
        """Session A state must not leak into session B."""
        rt._session_skill_tools['sess-A'] = {SKILL_ID: list(SKILL_TOOLS)}

        captured_tools = []

        def capture_and_respond(*args, **kwargs):
            captured_tools.extend(kwargs.get('tools') or [])
            return _llm_text()

        rt._mock_llm.chat_completion.side_effect = capture_and_respond
        agent = {'id': AGENT_ID}
        ctx = {'id': AGENT_ID, 'user_id': 'u', 'channel_id': None,
               'session_id': 'sess-B', 'assigned_tool_ids': [], 'is_super': False}
        rt._run_tool_loop(agent, ctx, [{'role': 'user', 'content': 'hi'}], [], 'sess-B')

        assert captured_tools == []


class TestLazySkillToolsSurvivePruning:
    """Lazy-loaded tools remain available after the normal pruning threshold."""

    def test_late_load_keeps_new_skill_tools_on_next_iteration(self, rt):
        captured_tool_names = []
        warmup_tool = {
            'type': 'function',
            'function': {
                'name': 'warmup_tool',
                'parameters': {'type': 'object', 'properties': {}, 'required': []},
            },
        }
        unused_tool = {
            'type': 'function',
            'function': {
                'name': 'unused_tool',
                'parameters': {'type': 'object', 'properties': {}, 'required': []},
            },
        }
        responses = iter([
            _llm_tool_call('warmup_tool', cid='warmup-1'),
            _llm_tool_call('warmup_tool', cid='warmup-2'),
            _llm_tool_call('use_skill', cid='late-load'),
            _llm_text(),
        ])

        def capture_and_respond(*args, **kwargs):
            captured_tool_names.append({
                tool['function']['name'] for tool in (kwargs.get('tools') or [])
            })
            return next(responses)

        rt._mock_llm.chat_completion.side_effect = capture_and_respond
        rt._mock_tr.get_builtin_executor.return_value = _use_skill_executor()
        agent = {'id': AGENT_ID}
        ctx = {
            'id': AGENT_ID, 'user_id': 'user-1', 'channel_id': None,
            'session_id': SESSION_ID, 'assigned_tool_ids': [], 'is_super': False,
        }
        rt._run_tool_loop(agent, ctx, [{'role': 'user', 'content': 'hi'}],
                          [warmup_tool, unused_tool], SESSION_ID)

        assert 'skill_tool_a' in captured_tool_names[3]
        assert 'warmup_tool' in captured_tool_names[3]
        assert 'unused_tool' not in captured_tool_names[3]

    def test_repeated_load_keeps_skill_tools_after_pruning_threshold(self, rt):
        captured_tool_names = []
        unused_tool = {
            'type': 'function',
            'function': {
                'name': 'unused_tool',
                'parameters': {'type': 'object', 'properties': {}, 'required': []},
            },
        }
        responses = iter([
            _llm_tool_call('use_skill', cid='load-1'),
            _llm_tool_call('use_skill', cid='load-2'),
            _llm_tool_call('use_skill', cid='load-3'),
            _llm_text(),
        ])

        def capture_and_respond(*args, **kwargs):
            captured_tool_names.append({
                tool['function']['name'] for tool in (kwargs.get('tools') or [])
            })
            return next(responses)

        rt._mock_llm.chat_completion.side_effect = capture_and_respond
        rt._mock_tr.get_builtin_executor.return_value = _use_skill_executor()
        agent = {'id': AGENT_ID}
        ctx = {
            'id': AGENT_ID, 'user_id': 'user-1', 'channel_id': None,
            'session_id': SESSION_ID, 'assigned_tool_ids': [], 'is_super': False,
        }
        rt._run_tool_loop(agent, ctx, [{'role': 'user', 'content': 'hi'}],
                          [unused_tool], SESSION_ID)

        assert 'skill_tool_a' in captured_tool_names[3]
        assert 'unused_tool' not in captured_tool_names[3]


class TestUnloadSkillClearsSessionState:
    """unload_skill → skill removed from session dicts."""

    def test_tools_removed_from_session(self, rt):
        rt._session_skill_tools[SESSION_ID] = {SKILL_ID: list(SKILL_TOOLS)}
        rt._session_skill_mds[SESSION_ID] = {SKILL_ID: SKILL_SYSTEM_MD}

        _run_turn(rt, [_llm_tool_call('unload_skill'), _llm_text()],
                  executor=_unload_skill_executor())

        assert SKILL_ID not in rt._session_skill_tools.get(SESSION_ID, {})

    def test_system_md_removed_from_session(self, rt):
        rt._session_skill_tools[SESSION_ID] = {SKILL_ID: list(SKILL_TOOLS)}
        rt._session_skill_mds[SESSION_ID] = {SKILL_ID: SKILL_SYSTEM_MD}

        _run_turn(rt, [_llm_tool_call('unload_skill'), _llm_text()],
                  executor=_unload_skill_executor())

        assert SKILL_ID not in rt._session_skill_mds.get(SESSION_ID, {})

    def test_other_skills_unaffected_by_unload(self, rt):
        """Unloading skill A must not remove skill B from session."""
        other_tool = [{'type': 'function', 'function': {'name': 'other_tool', 'parameters': {'type': 'object', 'properties': {}}}}]
        rt._session_skill_tools[SESSION_ID] = {
            SKILL_ID: list(SKILL_TOOLS),
            'other_skill': other_tool,
        }
        rt._session_skill_mds[SESSION_ID] = {
            SKILL_ID: SKILL_SYSTEM_MD,
            'other_skill': '# Other skill',
        }

        _run_turn(rt, [_llm_tool_call('unload_skill'), _llm_text()],
                  executor=_unload_skill_executor())

        assert 'other_skill' in rt._session_skill_tools.get(SESSION_ID, {})
        assert 'other_skill' in rt._session_skill_mds.get(SESSION_ID, {})


class TestClearSessionRemovesSkillState:
    """clear_session → both session dicts cleaned up for that session."""

    def test_skill_mds_cleared(self, rt):
        rt._session_skill_mds[SESSION_ID] = {SKILL_ID: SKILL_SYSTEM_MD}
        with patch('backend.agent_runtime.runtime.db') as mock_db:
            mock_db.get_or_create_session.return_value = SESSION_ID
            mock_db.clear_session.return_value = None
            rt.clear_session(AGENT_ID, 'user-1')
        assert SESSION_ID not in rt._session_skill_mds

    def test_skill_tools_cleared(self, rt):
        rt._session_skill_tools[SESSION_ID] = {SKILL_ID: list(SKILL_TOOLS)}
        with patch('backend.agent_runtime.runtime.db') as mock_db:
            mock_db.get_or_create_session.return_value = SESSION_ID
            mock_db.clear_session.return_value = None
            rt.clear_session(AGENT_ID, 'user-1')
        assert SESSION_ID not in rt._session_skill_tools

    def test_other_session_unaffected_by_clear(self, rt):
        rt._session_skill_mds['sess-other'] = {SKILL_ID: SKILL_SYSTEM_MD}
        rt._session_skill_mds[SESSION_ID] = {SKILL_ID: SKILL_SYSTEM_MD}
        with patch('backend.agent_runtime.runtime.db') as mock_db:
            mock_db.get_or_create_session.return_value = SESSION_ID
            mock_db.clear_session.return_value = None
            rt.clear_session(AGENT_ID, 'user-1')
        assert 'sess-other' in rt._session_skill_mds
