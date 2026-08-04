"""
Unit tests for llm_loop recovery mechanisms:
- _emergency_compact_messages: context compaction on exceed_context_size_error
- Trivial response filter (">", "<", etc.)
- Empty response recovery sentinel injection
- Context-size error triggers compaction + retry in run_tool_loop
"""

import sys
import os
import types
import json
import threading
import unittest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# backend.agent_runtime.__init__ instantiates a global AgentRuntime() (starts
# non-daemon queue workers) so importing the package hangs the test process.
# Strategy: pre-stub backend.agent_runtime as a bare ModuleType (with __path__
# set so submodule lookups work), load the three submodules that llm_loop.py
# imports at module level (llm_call, llm_response_parser, llm_tool_executor)
# via importlib directly, then load llm_loop.py the same way.
import importlib.util as _ilu
import backend as _backend_pkg

_AR_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'backend', 'agent_runtime',
)

# Save whatever is currently in sys.modules so teardown can restore it.
_SAVED_AR = sys.modules.get('backend.agent_runtime')
_SAVED_AR_SUBKEYS = {
    k: v for k, v in sys.modules.items()
    if k.startswith('backend.agent_runtime.')
}

# Only install the stub if the real package isn't already loaded.
# If it IS already loaded (from another test that ran first), we leave it
# alone — non-daemon threads are already running and we can't undo that,
# but we also don't need to trigger a second instantiation.
if not isinstance(_SAVED_AR, types.ModuleType) or not hasattr(_SAVED_AR, 'AgentRuntime'):
    _ar_stub = types.ModuleType('backend.agent_runtime')
    _ar_stub.__path__ = [_AR_PATH]
    _ar_stub.__package__ = 'backend.agent_runtime'
    sys.modules['backend.agent_runtime'] = _ar_stub
    _backend_pkg.agent_runtime = _ar_stub
    # Add agent_runtime attribute so patch('backend.agent_runtime.agent_runtime') works
    # in other test files (e.g. test_scheduler.py) that run after this module is loaded.
    # Add AgentRuntime class stub so test_skill_session_persistence.py's fixture guard
    # (which checks hasattr(..., 'AgentRuntime')) does NOT delete our stub.
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

# If llm_loop is already loaded (the real package was imported by an earlier test
# module), reuse that exact module object.  runtime.py binds it once via
# `from backend.agent_runtime import llm_loop as _loop`, so exec'ing a duplicate
# here would replace the sys.modules entry with a divergent copy while the live
# AgentRuntime keeps using the original.  That breaks
# patch('backend.agent_runtime.llm_loop.*') for later test modules
# (e.g. test_skill_session_persistence.py), since the patch hits our copy but the
# runtime runs the original.
_existing_loop = sys.modules.get('backend.agent_runtime.llm_loop')
if isinstance(_existing_loop, types.ModuleType) and hasattr(_existing_loop, '_emergency_compact_messages'):
    _llm_loop_mod = _existing_loop
    _ar_stub.llm_loop = _llm_loop_mod
else:
    _llm_loop_path = os.path.join(_AR_PATH, 'llm_loop.py')
    _spec = _ilu.spec_from_file_location('backend.agent_runtime.llm_loop', _llm_loop_path)
    _llm_loop_mod = _ilu.module_from_spec(_spec)
    sys.modules['backend.agent_runtime.llm_loop'] = _llm_loop_mod
    _ar_stub.llm_loop = _llm_loop_mod
    _spec.loader.exec_module(_llm_loop_mod)
_emergency_compact_messages = _llm_loop_mod._emergency_compact_messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(content='done'):
    return {
        'success': True,
        'response': {'choices': [{'message': {'content': content, 'tool_calls': None}, 'finish_reason': 'stop'}]},
        'duration_ms': 10,
    }


def _err(detail='request (60000 tokens) exceeds the available context size (49152 tokens), type=exceed_context_size_error'):
    return {
        'success': False,
        'error_type': 'llm_error',
        'error_detail': detail,
        'response': {},
    }


def _make_messages(with_summary=True, n_conv=10):
    """Build a realistic messages list for compaction tests."""
    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
    ]
    if with_summary:
        msgs.append({"role": "system", "content": "## Prior conversation summary\n- User asked about dark mode\n- Agent implemented CSS changes"})
    msgs.append({"role": "system", "content": "## Long-term Memory\n- User name: Andi Wijaya"})

    for i in range(n_conv):
        msgs.append({"role": "user", "content": f"User message {i}"})
        msgs.append({"role": "assistant", "content": f"Assistant reply {i}"})

    return msgs


# ---------------------------------------------------------------------------
# Tests: _emergency_compact_messages
# ---------------------------------------------------------------------------

class TestEmergencyCompactMessages(unittest.TestCase):

    def _make_llm(self, summary_reply='- Compacted point 1\n- Compacted point 2'):
        llm = MagicMock()
        llm.chat_completion.return_value = {
            'success': True,
            'response': {'choices': [{'message': {'content': summary_reply}, 'finish_reason': 'stop'}]},
        }
        return llm

    def test_returns_shorter_messages_list(self):
        messages = _make_messages(with_summary=True, n_conv=20)
        llm = self._make_llm()
        lock = threading.Lock()

        result = _emergency_compact_messages(messages, llm, lock, 'sess1', 'agent1')

        self.assertIsNotNone(result)
        self.assertLess(len(result), len(messages))

    def test_system_prompt_preserved_as_first_message(self):
        messages = _make_messages(with_summary=True, n_conv=10)
        llm = self._make_llm()
        result = _emergency_compact_messages(messages, llm, threading.Lock(), 'sess1', 'agent1')

        self.assertEqual(result[0]['role'], 'system')
        self.assertIn('helpful assistant', result[0]['content'])

    def test_compacted_summary_injected(self):
        messages = _make_messages(with_summary=True, n_conv=10)
        llm = self._make_llm('- Only relevant point')
        result = _emergency_compact_messages(messages, llm, threading.Lock(), 'sess1', 'agent1')

        summary_msgs = [m for m in result if '## Prior conversation summary' in (m.get('content') or '')]
        self.assertEqual(len(summary_msgs), 1)
        self.assertIn('Only relevant point', summary_msgs[0]['content'])
        self.assertIn('compacted', summary_msgs[0]['content'])

    def test_keeps_at_most_5_conversation_entries(self):
        messages = _make_messages(with_summary=True, n_conv=20)
        llm = self._make_llm()
        result = _emergency_compact_messages(messages, llm, threading.Lock(), 'sess1', 'agent1')

        conv_msgs = [m for m in result if m.get('role') in ('user', 'assistant')]
        self.assertLessEqual(len(conv_msgs), 5)

    def test_no_tool_messages_in_result(self):
        messages = _make_messages(with_summary=True, n_conv=5)
        # Add some tool messages
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "bash", "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": "c1", "content": "tool result"})
        llm = self._make_llm()
        result = _emergency_compact_messages(messages, llm, threading.Lock(), 'sess1', 'agent1')

        self.assertFalse(any(m.get('role') == 'tool' for m in result))
        self.assertFalse(any(m.get('tool_calls') for m in result))

    def test_works_without_existing_summary(self):
        messages = _make_messages(with_summary=False, n_conv=10)
        llm = self._make_llm('- Summary from scratch')
        result = _emergency_compact_messages(messages, llm, threading.Lock(), 'sess1', 'agent1')

        self.assertIsNotNone(result)
        summary_msgs = [m for m in result if '## Prior conversation summary' in (m.get('content') or '')]
        self.assertEqual(len(summary_msgs), 1)

    def test_returns_none_on_llm_failure(self):
        messages = _make_messages(with_summary=True, n_conv=10)
        llm = MagicMock()
        llm.chat_completion.return_value = {
            'success': False,
            'error_type': 'api_error',
            'error_detail': 'timeout',
            'response': {},
        }
        result = _emergency_compact_messages(messages, llm, threading.Lock(), 'sess1', 'agent1')
        self.assertIsNone(result)

    def test_returns_none_on_empty_llm_output(self):
        messages = _make_messages(with_summary=True, n_conv=10)
        llm = MagicMock()
        llm.chat_completion.return_value = {
            'success': True,
            'response': {'choices': [{'message': {'content': ''}, 'finish_reason': 'stop'}]},
        }
        result = _emergency_compact_messages(messages, llm, threading.Lock(), 'sess1', 'agent1')
        self.assertIsNone(result)

    def test_returns_none_on_llm_exception(self):
        messages = _make_messages(with_summary=True, n_conv=10)
        llm = MagicMock()
        llm.chat_completion.side_effect = RuntimeError('connection refused')
        result = _emergency_compact_messages(messages, llm, threading.Lock(), 'sess1', 'agent1')
        self.assertIsNone(result)

    def test_other_system_messages_preserved(self):
        """Long-term Memory system message should survive compaction."""
        messages = _make_messages(with_summary=True, n_conv=10)
        llm = self._make_llm()
        result = _emergency_compact_messages(messages, llm, threading.Lock(), 'sess1', 'agent1')

        memory_msgs = [m for m in result if '## Long-term Memory' in (m.get('content') or '')]
        self.assertEqual(len(memory_msgs), 1)

    def test_llm_prompt_contains_summary_and_recent(self):
        """Verify the compaction prompt contains both the summary and recent conversation."""
        messages = _make_messages(with_summary=True, n_conv=6)
        llm = self._make_llm()
        _emergency_compact_messages(messages, llm, threading.Lock(), 'sess1', 'agent1')

        call_args = llm.chat_completion.call_args
        prompt_content = call_args[1]['messages'][0]['content']
        self.assertIn('Existing Summary', prompt_content)
        self.assertIn('Recent Conversation', prompt_content)
        self.assertIn('30%', prompt_content)


# ---------------------------------------------------------------------------
# Tests: trivial response filter + empty response recovery
# ---------------------------------------------------------------------------

class TestEmptyResponseRecovery(unittest.TestCase):
    """Test the trivial-response filter and sentinel injection in run_tool_loop."""

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

    def _run_tool_loop(self, llm, messages, session_id, extra_db_attrs=None):
        """Run run_tool_loop with patched db/tool_registry/LLMClient/event_stream."""
        run_tool_loop = _llm_loop_mod.run_tool_loop
        mock_db = MagicMock()
        mock_db.get_setting.side_effect = lambda key, default=None: default or '0'
        mock_db.add_chat_message.return_value = None
        mock_db.upsert_agent_state.return_value = None
        mock_db.get_agent_default_model.return_value = None
        if extra_db_attrs:
            for k, v in extra_db_attrs.items():
                setattr(mock_db, k, v)
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
                tools=[],
                session_id=session_id,
                llm_lock=threading.Lock(),
                stop_event=threading.Event(),
                session_skill_mds={},
                session_skill_tools={},
                llm_log_path=None,
            )

    def test_trivial_response_treated_as_empty(self):
        """'>' response should be treated as empty and trigger recovery sentinel."""
        llm = MagicMock()
        llm.chat_completion.side_effect = [
            # First call: trivial response ">"
            {'success': True, 'response': {'choices': [{'message': {'content': '>', 'tool_calls': None}, 'finish_reason': 'stop'}]}, 'duration_ms': 10},
            # Second call (after sentinel): proper response
            _ok('Here is my answer.'),
        ]
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        result, _, _ = self._run_tool_loop(llm, messages, 'sess1')
        self.assertEqual(llm.chat_completion.call_count, 2)
        self.assertIn('answer', str(result))

    def test_empty_response_sentinel_injected_once(self):
        """Empty response should inject sentinel and retry, up to 2 times."""
        llm = MagicMock()
        llm.chat_completion.side_effect = [
            _ok(''),           # empty
            _ok(''),           # empty again (2nd injection)
            _ok('Final answer'),  # should not reach — max 2 injections, then return
        ]
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        result, _, _ = self._run_tool_loop(llm, messages, 'sess2')
        # 1 initial + 2 recovery = 3 calls max (or fewer if loop exits)
        self.assertLessEqual(llm.chat_completion.call_count, 3)

    def test_exhausted_empty_surfaces_visible_final(self):
        """Regression #642: when recovery exhausts with empty content, the turn must
        still surface a visible '(No response)' final — a saved assistant message and
        an is_final response chunk — so chat-ui renders a bubble instead of hanging."""
        llm = MagicMock()
        llm.chat_completion.side_effect = [_ok(''), _ok(''), _ok('')]  # always empty
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]

        run_tool_loop = _llm_loop_mod.run_tool_loop
        mock_db = MagicMock()
        mock_db.get_setting.side_effect = lambda key, default=None: default or '0'
        mock_db.get_agent_default_model.return_value = None
        mock_tr = MagicMock()
        mock_tr.get_builtin_executor.return_value = lambda n, a: None
        mock_tr.get_real_executor.return_value = lambda n, a: None
        import backend.event_stream as _es_mod
        with patch.object(_llm_loop_mod, 'db', mock_db), \
             patch.object(_llm_loop_mod, 'tool_registry', mock_tr), \
             patch.object(_es_mod, 'event_stream', MagicMock()) as mock_es, \
             patch.object(_llm_loop_mod, 'LLMClient', return_value=llm), \
             patch.object(_llm_loop_mod, 'llm_client', llm):
            result, _, _ = run_tool_loop(
                agent=self._make_agent(), agent_context=self._make_agent_context(),
                messages=messages, tools=[], session_id='sess_empty',
                llm_lock=threading.Lock(), stop_event=threading.Event(),
                session_skill_mds={}, session_skill_tools={}, llm_log_path=None,
            )

        self.assertEqual(result, "(No response)")
        # The placeholder is persisted as the assistant message (was skipped pre-fix).
        saved = [c for c in mock_db.add_chat_message.call_args_list
                 if len(c[0]) >= 3 and c[0][1] == 'assistant' and c[0][2] == "(No response)"]
        self.assertTrue(saved, "expected a saved '(No response)' assistant message")
        # An is_final response chunk must be emitted so SSE-mode renders the bubble.
        final_chunks = [c for c in mock_es.emit.call_args_list
                        if c[0][0] == 'llm_response_chunk' and c[0][1].get('is_final')
                        and c[0][1].get('content') == "(No response)"]
        self.assertTrue(final_chunks, "expected an is_final llm_response_chunk with placeholder")


# ---------------------------------------------------------------------------
# Tests: context-size error triggers compaction + retry
# ---------------------------------------------------------------------------

class TestContextSizeCompaction(unittest.TestCase):

    def _make_agent(self):
        return {
            'id': 'agent1', 'name': 'Test', 'model': None,
            'send_intermediate_responses': False, 'summarize_threshold': 0,
        }

    def _make_agent_context(self):
        return {'user_id': 'u1', 'channel_id': 'ch1', 'is_super': False, 'agent_state': None}

    def _run_tool_loop(self, llm, messages, session_id, extra_db_attrs=None):
        """Run run_tool_loop with patched db/tool_registry/LLMClient/event_stream."""
        run_tool_loop = _llm_loop_mod.run_tool_loop
        mock_db = MagicMock()
        mock_db.get_setting.side_effect = lambda key, default=None: default or '0'
        mock_db.add_chat_message.return_value = None
        mock_db.get_agent_default_model.return_value = None
        mock_db.get_agent_fallback_model.return_value = None
        mock_db.get_agent_model.return_value = None
        mock_db.get_model_by_id.return_value = None
        mock_db.get_summary.return_value = None
        mock_db.upsert_summary.return_value = None
        if extra_db_attrs:
            for k, v in extra_db_attrs.items():
                setattr(mock_db, k, v)
        mock_tr = MagicMock()
        mock_tr.get_builtin_executor.return_value = lambda n, a: None
        mock_tr.get_real_executor.return_value = lambda n, a: None
        import backend.event_stream as _es_mod
        with patch.object(_llm_loop_mod, 'db', mock_db), \
             patch.object(_llm_loop_mod, 'tool_registry', mock_tr), \
             patch.object(_es_mod, 'event_stream', MagicMock()) as mock_es, \
             patch.object(_llm_loop_mod, 'LLMClient', return_value=llm), \
             patch.object(_llm_loop_mod, 'llm_client', llm):
            result = run_tool_loop(
                agent=self._make_agent(),
                agent_context=self._make_agent_context(),
                messages=messages,
                tools=[],
                session_id=session_id,
                llm_lock=threading.Lock(),
                stop_event=threading.Event(),
                session_skill_mds={},
                session_skill_tools={},
                llm_log_path=None,
            )
            return result, mock_es

    def test_context_error_triggers_compaction_then_retry(self):
        """exceed_context_size_error should compact messages and retry successfully."""
        llm = MagicMock()
        llm.chat_completion.side_effect = [
            _err('request (60000 tokens) exceeds the available context size (49152 tokens), type=exceed_context_size_error'),
            _ok('- Compacted summary bullet'),
            _ok('Task done after compaction.'),
        ]
        messages = _make_messages(with_summary=True, n_conv=10)
        (result, _, _), _ = self._run_tool_loop(llm, messages, 'sess3')
        self.assertEqual(llm.chat_completion.call_count, 3)
        self.assertIn('compaction', str(result).lower() + 'task done after compaction.')

    def test_context_error_compaction_fails_returns_error(self):
        """If compaction LLM call fails, return the humanized error message."""
        llm = MagicMock()
        llm.chat_completion.side_effect = [
            _err('exceeds the available context size'),
            {'success': False, 'error_type': 'api_error', 'error_detail': 'timeout', 'response': {}},
        ]
        # Use n_conv=3 (6 conversation messages) so that the dumb-truncation
        # safety net in Fix 3 is a no-op (_keep_n=6, 6 > 6 is False).
        # Otherwise dumb-truncation would fire after compaction failure,
        # issue a 3rd chat_completion call, and exhaust the mock's side_effect.
        messages = _make_messages(with_summary=False, n_conv=3)
        (result, _, _), _ = self._run_tool_loop(llm, messages, 'sess4')
        self.assertTrue(result.get('error'))
        self.assertIn('too long', result.get('text', '').lower())

    def test_context_error_no_infinite_loop(self):
        """Compaction only attempted once — second context error returns error."""
        llm = MagicMock()
        llm.chat_completion.side_effect = [
            _err('exceeds the available context size'),
            _ok('- summary'),
            _err('exceeds the available context size'),
            _ok('This should not be reached'),
        ]
        messages = _make_messages(with_summary=True, n_conv=5)
        (result, _, _), _ = self._run_tool_loop(llm, messages, 'sess5')
        self.assertEqual(llm.chat_completion.call_count, 3)
        self.assertTrue(result.get('error'))

    def test_compaction_emits_llm_retry_events(self):
        """Should emit llm_retry events for user notification during compaction."""
        llm = MagicMock()
        llm.chat_completion.side_effect = [
            _err('exceeds the available context size'),
            _ok('- summary'),
            _ok('All done.'),
        ]
        messages = _make_messages(with_summary=True, n_conv=5)
        (_, _, _), mock_es = self._run_tool_loop(llm, messages, 'sess6')

        emitted_events = [c[0][0] for c in mock_es.emit.call_args_list]
        retry_events = [e for e in emitted_events if e == 'llm_retry']
        self.assertGreaterEqual(len(retry_events), 2)

        retry_payloads = [c[0][1] for c in mock_es.emit.call_args_list if c[0][0] == 'llm_retry']
        user_msgs = [p.get('user_message', '').lower() for p in retry_payloads]
        self.assertTrue(any('compact' in m for m in user_msgs))
        self.assertTrue(any('resuming' in m or 'summary' in m for m in user_msgs))


# ---------------------------------------------------------------------------
# Tests: orphaned tool_call/tool pair sanitization (400 "insufficient tool
# messages" recovery)
# ---------------------------------------------------------------------------

def _asst_tc(call_id, name='read_file'):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": "{}"}}]}


def _tc_err(detail="LLM API error: 400 - An assistant message with 'tool_calls' "
                   "must be followed by tool messages responding to each 'tool_call_id'."):
    return {'success': False, 'error_type': 'api_error', 'error_detail': detail, 'response': {}}


class TestSanitizeToolCallPairs(unittest.TestCase):
    """Unit tests for _sanitize_tool_call_pairs."""

    def setUp(self):
        self.sanitize = _llm_loop_mod._sanitize_tool_call_pairs

    def test_noop_on_well_formed_history(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            _asst_tc("c1"),
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
            {"role": "assistant", "content": "done"},
        ]
        original = [dict(m) for m in messages]
        changed = self.sanitize(messages)
        self.assertFalse(changed)
        self.assertEqual(messages, original)

    def test_injects_synthetic_response_for_orphaned_tool_calls(self):
        messages = [
            {"role": "user", "content": "hi"},
            _asst_tc("c1"),
            # No tool response for c1 — orphaned.
            {"role": "user", "content": "next"},
        ]
        changed = self.sanitize(messages)
        self.assertTrue(changed)
        # A synthetic tool response for c1 must now follow the assistant message.
        tool_msgs = [m for m in messages if m.get('role') == 'tool']
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]['tool_call_id'], 'c1')
        self.assertIn('interrupted', tool_msgs[0]['content'])
        # Position: synthetic response immediately after its assistant message.
        idx = next(i for i, m in enumerate(messages) if m.get('tool_calls'))
        self.assertEqual(messages[idx + 1]['role'], 'tool')

    def test_drops_orphaned_tool_message(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "ghost", "content": "stray"},
            {"role": "assistant", "content": "done"},
        ]
        changed = self.sanitize(messages)
        self.assertTrue(changed)
        self.assertFalse(any(m.get('role') == 'tool' for m in messages))

    def test_partial_tool_responses_filled(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "",
             "tool_calls": [
                 {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                 {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
             ]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            # c2 missing
        ]
        changed = self.sanitize(messages)
        self.assertTrue(changed)
        responded = {m['tool_call_id'] for m in messages if m.get('role') == 'tool'}
        self.assertEqual(responded, {"c1", "c2"})

    def test_drops_duplicate_tool_response(self):
        messages = [
            _asst_tc("c1"),
            {"role": "tool", "tool_call_id": "c1", "content": "first"},
            {"role": "tool", "tool_call_id": "c1", "content": "dup"},
        ]
        changed = self.sanitize(messages)
        self.assertTrue(changed)
        tool_msgs = [m for m in messages if m.get('role') == 'tool']
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]['content'], 'first')


class TestApiErrorOrphanRecovery(unittest.TestCase):
    """run_tool_loop should repair orphaned tool_calls and retry on a 400 api_error."""

    def _make_agent_context(self):
        return {'user_id': 'u1', 'channel_id': 'ch1', 'is_super': False, 'agent_state': None}

    def _make_agent(self, agent_id='test_agent'):
        return {'id': agent_id, 'name': 'Test', 'model': None,
                'send_intermediate_responses': False, 'summarize_threshold': 0}

    def _run_tool_loop(self, llm, messages, session_id, tools=None,
                       fallback_model=None, fallback_llm=None, return_db=False):
        run_tool_loop = _llm_loop_mod.run_tool_loop
        mock_db = MagicMock()
        mock_db.get_setting.side_effect = lambda key, default=None: default or '0'
        mock_db.add_chat_message.return_value = None
        mock_db.get_agent_default_model.return_value = None
        mock_db.get_agent_model.return_value = None
        mock_db.get_agent_state.return_value = None
        mock_db.get_agent_fallback_model.return_value = fallback_model
        mock_db.get_summary.return_value = None
        mock_tr = MagicMock()
        mock_tr.get_builtin_executor.return_value = lambda n, a: None
        mock_tr.get_real_executor.return_value = lambda n, a: None
        clients = [fallback_llm] if fallback_llm is not None else [llm]
        import backend.event_stream as _es_mod
        with patch.object(_llm_loop_mod, 'db', mock_db), \
             patch.object(_llm_loop_mod, 'tool_registry', mock_tr), \
             patch.object(_es_mod, 'event_stream', MagicMock()) as mock_es, \
             patch.object(_llm_loop_mod, 'LLMClient', side_effect=clients), \
             patch.object(_llm_loop_mod, 'llm_client', llm):
            result = run_tool_loop(
                agent=self._make_agent(),
                agent_context=self._make_agent_context(),
                messages=messages,
                tools=tools or [],
                session_id=session_id,
                llm_lock=threading.Lock(),
                stop_event=threading.Event(),
                session_skill_mds={},
                session_skill_tools={},
                llm_log_path=None,
            )
            if return_db:
                return (result, mock_db), mock_es
            return result, mock_es

    def test_proactive_sanitize_prevents_400(self):
        """A pre-existing orphaned tool_calls in history is repaired before the
        first call, so the LLM call succeeds on the first try."""
        llm = MagicMock()
        llm.chat_completion.side_effect = [_ok('All good.')]
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            _asst_tc("c1"),  # orphaned — no tool response
            {"role": "user", "content": "continue"},
        ]
        (result, _, _), _ = self._run_tool_loop(llm, messages, 'sess_orphan1')
        self.assertEqual(llm.chat_completion.call_count, 1)
        # History sent to the model now contains a synthetic tool response.
        sent = llm.chat_completion.call_args[1]['messages']
        self.assertTrue(any(m.get('role') == 'tool' and m.get('tool_call_id') == 'c1'
                            for m in sent))
        self.assertIn('all good', str(result).lower())

    def test_api_error_returns_humanized_final_answer(self):
        """A 400 api_error that proactive sanitize can't fix returns a humanized
        message AND emits a final_answer event (error path), without looping."""
        llm = MagicMock()
        llm.chat_completion.side_effect = [_tc_err(), _tc_err(), _tc_err(), _tc_err()]
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},  # no orphan — sanitizer is a no-op
        ]
        (result, _, _), mock_es = self._run_tool_loop(llm, messages, 'sess_orphan3')
        self.assertTrue(result.get('error'))
        self.assertIn('repaired', result.get('text', '').lower())
        # final_answer must be emitted on the error path (Fix C).
        emitted = [c[0][0] for c in mock_es.emit.call_args_list]
        self.assertIn('final_answer', emitted)
        # Bounded — must not loop forever.
        self.assertLessEqual(llm.chat_completion.call_count, 3)


class TestActiveContextShadowIntegration(TestApiErrorOrphanRecovery):
    """Shadow projection emits attribution without changing provider messages."""

    def test_shadow_mode_keeps_canonical_messages_on_wire(self):
        llm = MagicMock()
        llm.chat_completion.return_value = _ok('Done.')
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go"},
            _asst_tc("old"),
            {"role": "tool", "tool_call_id": "old", "content": "large result " * 1000},
            _asst_tc("frontier"),
            {"role": "tool", "tool_call_id": "frontier", "content": "recent"},
        ]
        canonical_snapshot = json.loads(json.dumps(messages))

        with patch.object(_llm_loop_mod, 'ACTIVE_CONTEXT_MODE', 'shadow'), \
             patch.object(_llm_loop_mod, 'ACTIVE_CONTEXT_SOFT_TOKENS', 0), \
             patch.object(_llm_loop_mod, 'ACTIVE_CONTEXT_RECENT_GROUPS', 1):
            (_, _, _), mock_es = self._run_tool_loop(llm, messages, 'sess_shadow')

        sent = llm.chat_completion.call_args[1]['messages']
        self.assertEqual(sent, canonical_snapshot)
        self.assertEqual(messages, canonical_snapshot)
        projections = [c[0][1] for c in mock_es.emit.call_args_list
                       if c[0][0] == 'active_context_projection']
        self.assertEqual(len(projections), 1)
        self.assertTrue(projections[0]['applied'])
        self.assertGreater(projections[0]['saved_tokens'], 0)


class TestEffectiveRequestIntegration(TestApiErrorOrphanRecovery):
    def _long_tool_loop(self, groups=8):
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "inspect"}]
        for i in range(groups):
            messages.extend([
                _asst_tc(f"call-{i}"),
                {"role": "tool", "tool_call_id": f"call-{i}",
                 "content": (f"result-{i} " * 500)},
            ])
        return messages

    def test_enforced_primary_uses_projected_messages_and_exact_telemetry(self):
        llm = MagicMock(provider='primary-provider', model='primary-model')
        llm.chat_completion.return_value = _ok('Done.')
        messages = self._long_tool_loop()
        canonical_snapshot = json.loads(json.dumps(messages))
        tools = [
            {"type": "function", "function": {"name": "read_file",
             "description": "read " * 200, "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "unused",
             "description": "unused " * 200, "parameters": {"type": "object"}}},
        ]

        with patch.object(_llm_loop_mod, 'ACTIVE_CONTEXT_MODE', 'enforced'), \
             patch.object(_llm_loop_mod, 'ACTIVE_CONTEXT_SOFT_TOKENS', 0), \
             patch.object(_llm_loop_mod, 'ACTIVE_CONTEXT_RECENT_GROUPS', 1), \
             patch.object(_llm_loop_mod, '_persist_context_usage') as persist:
            (_, _, _), mock_es = self._run_tool_loop(
                llm, messages, 'sess_effective_primary', tools=tools)

        sent = llm.chat_completion.call_args.kwargs
        self.assertLess(len(sent['messages']), len(messages))
        self.assertEqual(messages, canonical_snapshot)
        projections = [c.args[1] for c in mock_es.emit.call_args_list
                       if c.args[0] == 'active_context_projection']
        metrics = projections[-1]
        self.assertEqual(metrics['path'], 'primary')
        self.assertEqual(metrics['provider'], 'primary-provider')
        self.assertLess(metrics['effective_message_tokens'],
                        metrics['canonical_message_tokens'] * 0.5)
        self.assertEqual(metrics['initial_tool_tokens'],
                         metrics['effective_tool_tokens'])
        usage = persist.call_args.args[2]
        self.assertEqual(usage['effective_request']['effective_message_tokens'],
                         metrics['effective_message_tokens'])
        self.assertEqual(usage['provider'], 'primary-provider')

    def test_tool_pruning_metrics_match_the_fourth_provider_request(self):
        llm = MagicMock(provider='primary-provider', model='primary-model')
        tool_rounds = []
        for i in range(3):
            tool_rounds.append({
                'success': True,
                'response': {'choices': [{'message': {
                    'content': '',
                    'tool_calls': [{'id': f'round-{i}', 'type': 'function',
                                    'function': {'name': 'read_file',
                                                 'arguments': '{}'}}]},
                    'finish_reason': 'tool_calls'}]},
                'duration_ms': 1,
            })
        llm.chat_completion.side_effect = tool_rounds + [_ok('Done.')]
        tools = [
            {"type": "function", "function": {"name": "read_file",
             "description": "used " * 200, "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "unused",
             "description": "unused " * 200, "parameters": {"type": "object"}}},
        ]

        with patch.object(_llm_loop_mod, 'ACTIVE_CONTEXT_MODE', 'off'):
            (_, _, _), mock_es = self._run_tool_loop(
                llm, [{"role": "user", "content": "go"}],
                'sess_effective_pruning', tools=tools)

        fourth = llm.chat_completion.call_args_list[3].kwargs
        self.assertEqual([t['function']['name'] for t in fourth['tools']], ['read_file'])
        requests = [c.args[1] for c in mock_es.emit.call_args_list
                    if c.args[0] == 'active_context_projection']
        fourth_metrics = requests[3]
        self.assertLess(fourth_metrics['effective_tool_tokens'],
                        fourth_metrics['initial_tool_tokens'])
        from backend.llm_usage_events import estimate_context_tokens
        self.assertEqual(fourth_metrics['effective_tool_tokens'],
                         estimate_context_tokens([], fourth['tools']))

    def test_context_retry_does_not_restore_projected_history(self):
        llm = MagicMock(provider='primary-provider', model='primary-model')
        llm.chat_completion.side_effect = [
            _err('exceeds the available context size'),
            _ok('- compacted effective request'),
            _ok('Done after retry.'),
        ]
        messages = self._long_tool_loop()
        canonical_snapshot = json.loads(json.dumps(messages))

        with patch.object(_llm_loop_mod, 'ACTIVE_CONTEXT_MODE', 'enforced'), \
             patch.object(_llm_loop_mod, 'ACTIVE_CONTEXT_SOFT_TOKENS', 0), \
             patch.object(_llm_loop_mod, 'ACTIVE_CONTEXT_RECENT_GROUPS', 1):
            self._run_tool_loop(llm, messages, 'sess_effective_retry')

        primary = llm.chat_completion.call_args_list[0].kwargs['messages']
        retry = llm.chat_completion.call_args_list[2].kwargs['messages']
        self.assertLess(len(primary), len(messages))
        self.assertLess(len(retry), len(messages))
        self.assertFalse(any('result-0' in str(m.get('content')) for m in retry))
        self.assertEqual(messages, canonical_snapshot)

    def test_fallback_reuses_projected_messages_and_tools(self):
        primary = MagicMock(provider='primary-provider', model='primary-model')
        primary.chat_completion.return_value = _tc_err('primary rejected request')
        fallback = MagicMock(provider='fallback-provider', model='fallback-model')
        fallback.chat_completion.return_value = _ok('Fallback done.')
        fallback_model = {
            'id': 'fallback-id', 'name': 'Fallback', 'provider': 'fallback-provider',
            'model_name': 'fallback-model', 'vision_supported': True,
        }
        messages = self._long_tool_loop()
        tools = [{"type": "function", "function": {"name": "read_file",
                  "parameters": {"type": "object"}}}]

        with patch.object(_llm_loop_mod, 'ACTIVE_CONTEXT_MODE', 'enforced'), \
             patch.object(_llm_loop_mod, 'ACTIVE_CONTEXT_SOFT_TOKENS', 0), \
             patch.object(_llm_loop_mod, 'ACTIVE_CONTEXT_RECENT_GROUPS', 1):
            (_, _, _), mock_es = self._run_tool_loop(
                primary, messages, 'sess_effective_fallback', tools=tools,
                fallback_model=fallback_model, fallback_llm=fallback)

        primary_call = primary.chat_completion.call_args.kwargs
        fallback_call = fallback.chat_completion.call_args.kwargs
        self.assertEqual(fallback_call['messages'], primary_call['messages'])
        self.assertIs(fallback_call['tools'], primary_call['tools'])
        requests = [c.args[1] for c in mock_es.emit.call_args_list
                    if c.args[0] == 'llm_effective_request']
        self.assertEqual(requests[-1]['path'], 'fallback')
        self.assertEqual(requests[-1]['provider'], 'fallback-provider')

    def test_projection_exception_fails_open_with_reason(self):
        llm = MagicMock(provider='primary-provider', model='primary-model')
        llm.chat_completion.return_value = _ok('Done.')
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "go"}]

        with patch('backend.agent_runtime.active_context.project_active_context',
                   side_effect=ValueError('invalid projection')):
            (_, _, _), mock_es = self._run_tool_loop(
                llm, messages, 'sess_effective_fail_open')

        self.assertEqual(llm.chat_completion.call_args.kwargs['messages'], messages)
        self.assertEqual(messages, [{"role": "system", "content": "sys"},
                                    {"role": "user", "content": "go"}])
        projections = [c.args[1] for c in mock_es.emit.call_args_list
                       if c.args[0] == 'active_context_projection']
        self.assertIn('ValueError: invalid projection',
                      projections[-1]['fail_open_reason'])


class TestHumanizeInsufficientToolMessages(unittest.TestCase):
    def test_maps_insufficient_tool_messages(self):
        humanize = _llm_loop_mod._humanize_llm_error
        msg = humanize("LLM API error: 400 - An assistant message with 'tool_calls' "
                       "must be followed by tool messages responding to each 'tool_call_id'.")
        self.assertIn('repaired', msg.lower())
        self.assertNotIn('400', msg)


if __name__ == '__main__':
    unittest.main()
