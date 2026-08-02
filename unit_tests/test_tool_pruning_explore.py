"""
Regression test for #713 tool-pruning regression (Explore tool silently pruned).

Root cause: _prune_tools() in backend/agent_runtime/llm_loop.py removes zero-call
tools after _TOOL_PRUNE_THRESHOLD (=3) iterations within a turn, unless they are
essential, belong to a loaded LAZY skill, or have been called. Eager skill tools
(explorer's Explore, direxplorer's Grep/Glob/Read) were unprotected, so the model
lost them for the rest of the turn and could never use them again.

Fix: protect tools from enabled EAGER skills (_eager_skill_fns) in the keep-condition.
"""

import sys
import os
import types
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util as _ilu
import backend as _backend_pkg

_AR_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'backend', 'agent_runtime',
)

_SAVED_AR = sys.modules.get('backend.agent_runtime')
_SAVED_AR_SUBKEYS = {
    k: v for k, v in sys.modules.items()
    if k.startswith('backend.agent_runtime.')
}

if not isinstance(_SAVED_AR, types.ModuleType) or not hasattr(_SAVED_AR, 'AgentRuntime'):
    _ar_stub = types.ModuleType('backend.agent_runtime')
    _ar_stub.__path__ = [_AR_PATH]
    _ar_stub.__package__ = 'backend.agent_runtime'
    sys.modules['backend.agent_runtime'] = _ar_stub
    _backend_pkg.agent_runtime = _ar_stub
    from unittest.mock import MagicMock as _MagicMock
    _ar_stub.agent_runtime = _MagicMock(name='agent_runtime_singleton')
    _ar_stub.AgentRuntime = _MagicMock(name='AgentRuntime')

    for _submod_name in ('llm_call', 'llm_response_parser', 'llm_tool_executor'):
        _submod_path = os.path.join(_AR_PATH, f'{_submod_name}.py')
        _submod_spec = _ilu.spec_from_file_location(
            f'backend.agent_runtime.{_submod_name}', _submod_path)
        _submod_mod = _ilu.module_from_spec(_submod_spec)
        sys.modules[f'backend.agent_runtime.{_submod_name}'] = _submod_mod
        setattr(_ar_stub, _submod_name, _submod_mod)
        _submod_spec.loader.exec_module(_submod_mod)
else:
    _ar_stub = _SAVED_AR

_existing_loop = sys.modules.get('backend.agent_runtime.llm_loop')
if isinstance(_existing_loop, types.ModuleType) and hasattr(_existing_loop, '_emergency_compact_messages'):
    _llm_loop_mod = _existing_loop
    _ar_stub.llm_loop = _llm_loop_mod
else:
    _loop_path = os.path.join(_AR_PATH, 'llm_loop.py')
    _loop_spec = _ilu.spec_from_file_location('backend.agent_runtime.llm_loop', _loop_path)
    _loop_mod = _ilu.module_from_spec(_loop_spec)
    sys.modules['backend.agent_runtime.llm_loop'] = _loop_mod
    setattr(_ar_stub, 'llm_loop', _loop_mod)
    _loop_spec.loader.exec_module(_loop_mod)
    _llm_loop_mod = _loop_mod


def _ok(content='done'):
    return {
        'success': True,
        'response': {'choices': [{'message': {'content': content, 'tool_calls': None}, 'finish_reason': 'stop'}]},
        'duration_ms': 10,
    }


def _tool_call(name='bash', call_id='c1'):
    return {
        'success': True,
        'response': {'choices': [{'message': {'content': None, 'tool_calls': [
            {'id': call_id, 'type': 'function',
             'function': {'name': name, 'arguments': '{}'}}
        ]}, 'finish_reason': 'tool_calls'}]},
        'duration_ms': 10,
    }


class TestToolPruningProtectsEagerSkillTools(unittest.TestCase):
    """Regression: Explore (eager skill tool) must survive _prune_tools at iteration >= threshold."""

    def _make_agent_context(self):
        return {'user_id': 'u1', 'channel_id': 'ch1', 'is_super': False, 'agent_state': None}

    def _make_agent(self, agent_id='test_agent'):
        return {
            'id': agent_id,
            'name': 'Test',
            'model': None,
            'send_intermediate_responses': False,
            'summarize_threshold': 0,
        }

    def _run_tool_loop(self, llm, messages, session_id, tools):
        run_tool_loop = _llm_loop_mod.run_tool_loop
        mock_db = MagicMock()
        mock_db.get_setting.side_effect = lambda key, default=None: default or '0'
        mock_db.add_chat_message.return_value = None
        mock_db.upsert_agent_state.return_value = None
        mock_db.get_agent_default_model.return_value = None
        mock_db.get_agent_model.return_value = None
        mock_db.get_agent_state.return_value = None
        mock_db.get_agent_fallback_model.return_value = None
        mock_db.get_summary.return_value = None
        mock_tr = MagicMock()
        mock_tr.get_builtin_executor.return_value = lambda n, a: None
        mock_tr.get_real_executor.return_value = lambda n, a: None
        import backend.event_stream as _es_mod
        with patch.object(_llm_loop_mod, 'db', mock_db), \
             patch.object(_llm_loop_mod, 'tool_registry', mock_tr), \
             patch.object(_es_mod, 'event_stream', MagicMock()), \
             patch.object(_llm_loop_mod, 'LLMClient', return_value=llm), \
             patch.object(_llm_loop_mod, 'llm_client', llm):
            return run_tool_loop(
                agent=self._make_agent(),
                agent_context=self._make_agent_context(),
                messages=messages,
                tools=tools,
                session_id=session_id,
                llm_lock=threading.Lock(),
                stop_event=threading.Event(),
                session_skill_mds={},
                session_skill_tools={},
                llm_log_path=None,
            )

    def test_explore_survives_pruning_after_threshold(self):
        """At iteration >= 3, Explore must still be in the tools passed to the LLM."""
        tool_defs = [
            {'type': 'function', 'function': {'name': 'Explore'}},
            {'type': 'function', 'function': {'name': 'bash'}},
            {'type': 'function', 'function': {'name': 'calculator'}},
        ]
        llm = MagicMock()
        # 4 tool-call rounds (pushes _iteration to 3), then final text answer.
        llm.chat_completion.side_effect = [
            _tool_call('bash', 'c1'),
            _tool_call('bash', 'c2'),
            _tool_call('bash', 'c3'),
            _tool_call('bash', 'c4'),
            _ok('Final answer'),
        ]
        messages = [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'go'}]
        result, _, _ = self._run_tool_loop(llm, messages, 'sess-prune-1', tool_defs)
        self.assertIn('Final answer', str(result))

        # Collect tools kwarg from every chat_completion call (skipping probe calls).
        tools_seen = []
        for c in llm.chat_completion.call_args_list:
            kw = c.kwargs or {}
            if kw.get('tools') is not None:
                tools_seen.append([t['function']['name'] for t in kw['tools']])
        self.assertGreaterEqual(len(tools_seen), 4, 'expected >=4 LLM calls with tools')

        # The LAST tool-bearing call is at iteration >= threshold — Explore must survive.
        last_tools = tools_seen[-1]
        self.assertIn('Explore', last_tools,
                      'Explore (eager skill tool) was pruned at iteration >= threshold: %s' % last_tools)
        # calculator (non-essential, zero calls) SHOULD be pruned — token optimization retained.
        self.assertNotIn('calculator', last_tools)


if __name__ == '__main__':
    unittest.main()
