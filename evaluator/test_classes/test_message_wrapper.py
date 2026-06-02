"""Unit tests for the Message Wrapper feature.

Tests configuration resolution, wrapper prefix application,
chatlog _wrapped flag passthrough, and system prompt protocol section.
No LLM output testing.
"""
import os
import sys
import json
import tempfile
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helper: import runtime functions without triggering full runtime singleton
# ---------------------------------------------------------------------------

# The package __init__ creates AgentRuntime() singleton at import time.
# We suppress the side-effect by clearing the package module before importing
# just the runtime submodule and patching the atexit registration.
for key in list(sys.modules.keys()):
    if key.startswith('backend.agent_runtime'):
        del sys.modules[key]

with patch('atexit.register'):  # suppress atexit from AgentRuntime.__init__
    from backend.agent_runtime.runtime import (
        _should_wrap_user_message,
        _apply_wrapper_prefix,
        WRAPPER_PREFIX,
    )
    from backend.agent_runtime.context import _build_static_prompt


# ---------------------------------------------------------------------------
# Tests: _should_wrap_user_message
# ---------------------------------------------------------------------------

class TestShouldWrapUserMessage:
    """Configuration resolution: per-agent > global > default(True)."""

    def test_per_agent_true_overrides_global_false(self):
        """per_agent=True, global='0' -> True"""
        agent = {'message_wrapper_enabled': True}
        with patch('models.db.db.get_setting', return_value='0'):
            result = _should_wrap_user_message(agent)
        assert result is True

    def test_per_agent_false_overrides_global_true(self):
        """per_agent=False, global='1' -> False"""
        agent = {'message_wrapper_enabled': False}
        with patch('models.db.db.get_setting', return_value='1'):
            result = _should_wrap_user_message(agent)
        assert result is False

    def test_per_agent_none_global_true(self):
        """per_agent=None, global='1' -> True"""
        agent = {}
        with patch('models.db.db.get_setting', return_value='1'):
            result = _should_wrap_user_message(agent)
        assert result is True

    def test_per_agent_none_global_false(self):
        """per_agent=None, global='0' -> False"""
        agent = {}
        with patch('models.db.db.get_setting', return_value='0'):
            result = _should_wrap_user_message(agent)
        assert result is False

    def test_per_agent_none_global_none_defaults_true(self):
        """per_agent=None, global=default('1') -> True"""
        agent = {}
        with patch('models.db.db.get_setting', return_value='1'):
            result = _should_wrap_user_message(agent)
        assert result is True

    def test_per_agent_explicit_1(self):
        """per_agent=1 (truthy int) -> True"""
        agent = {'message_wrapper_enabled': 1}
        with patch('models.db.db.get_setting', return_value='0'):
            result = _should_wrap_user_message(agent)
        assert result is True

    def test_per_agent_explicit_0(self):
        """per_agent=0 (falsy int) -> False"""
        agent = {'message_wrapper_enabled': 0}
        with patch('models.db.db.get_setting', return_value='1'):
            result = _should_wrap_user_message(agent)
        assert result is False


# ---------------------------------------------------------------------------
# Tests: _apply_wrapper_prefix
# ---------------------------------------------------------------------------

class TestApplyWrapperPrefix:
    """Wrapper prefix injection into user messages."""

    def test_wraps_last_user_message(self):
        """Last user message always gets wrapped when enabled."""
        msgs = [
            {'role': 'system', 'content': 'You are helpful.'},
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi!'},
            {'role': 'user', 'content': 'What is 2+2?'},
        ]
        _apply_wrapper_prefix(msgs, enabled=True)
        assert msgs[3]['content'].startswith(WRAPPER_PREFIX)
        assert 'What is 2+2?' in msgs[3]['content']
        # Non-last user message should NOT be wrapped
        assert msgs[1]['content'] == 'Hello'

    def test_wraps_historical_wrapped_messages(self):
        """Messages with _wrapped=True get wrapped even if not last."""
        msgs = [
            {'role': 'user', 'content': 'I like pizza', '_wrapped': True},
            {'role': 'assistant', 'content': 'Noted.'},
            {'role': 'user', 'content': 'And pasta'},
        ]
        _apply_wrapper_prefix(msgs, enabled=True)
        assert msgs[0]['content'].startswith(WRAPPER_PREFIX)
        assert 'I like pizza' in msgs[0]['content']
        assert msgs[2]['content'].startswith(WRAPPER_PREFIX)

    def test_cleans_wrapped_key(self):
        """_wrapped key is removed from messages after processing."""
        msgs = [
            {'role': 'user', 'content': 'Hi', '_wrapped': True},
        ]
        _apply_wrapper_prefix(msgs, enabled=True)
        assert '_wrapped' not in msgs[0]

    def test_disabled_does_nothing(self):
        """When enabled=False, no messages are modified."""
        msgs = [
            {'role': 'user', 'content': 'Hello'},
        ]
        original = [dict(m) for m in msgs]
        _apply_wrapper_prefix(msgs, enabled=False)
        assert msgs == original

    def test_empty_list(self):
        """Empty message list is handled gracefully."""
        msgs = []
        _apply_wrapper_prefix(msgs, enabled=True)
        assert msgs == []

    def test_only_system_and_assistant(self):
        """List without user messages is unchanged."""
        msgs = [
            {'role': 'system', 'content': 'Prompt'},
            {'role': 'assistant', 'content': 'Response'},
        ]
        original = [dict(m) for m in msgs]
        _apply_wrapper_prefix(msgs, enabled=True)
        assert msgs == original

    def test_wrapper_prefix_is_english(self):
        """Wrapper prefix is always in English regardless of user language."""
        assert 'Preference check' in WRAPPER_PREFIX
        assert 'remember()' in WRAPPER_PREFIX
        assert 'notes.md' in WRAPPER_PREFIX


# ---------------------------------------------------------------------------
# Tests: chatlog _wrapped flag passthrough
# ---------------------------------------------------------------------------

class TestChatlogWrappedFlag:
    """_wrapped metadata flag is passed through to LLM message dict."""

    def _make_chatlog(self, entries: list) -> object:
        """Create a ChatLog backed by a real JSONL temp file."""
        from models.chatlog import ChatLog
        self._tmpdir = tempfile.TemporaryDirectory()
        log_path = os.path.join(self._tmpdir.name, 'test.jsonl')
        with open(log_path, 'w') as f:
            for entry in entries:
                f.write(json.dumps(entry) + '\n')
        cl = ChatLog.__new__(ChatLog)
        cl._path = log_path
        cl._fh = None
        cl._lock = __import__('threading').Lock()
        cl._entries = []
        cl._dirty = False
        return cl

    def test_wrapped_true_in_metadata_produces_wrapped_flag(self):
        """When entry metadata has wrapped=True, _wrapped:True in output msg."""
        cl = self._make_chatlog([
            {
                'type': 'user',
                'content': 'Hello',
                'ts': 1000000,
                'session_id': 'test_session',
                'metadata': {'wrapped': True},
            },
        ])
        result = cl.get_entries_for_llm()
        assert len(result) == 1
        assert result[0]['role'] == 'user'
        assert result[0]['content'] == 'Hello'
        assert result[0].get('_wrapped') is True

    def test_wrapped_not_in_metadata_produces_no_flag(self):
        """When entry metadata has no wrapped key, no _wrapped in output."""
        cl = self._make_chatlog([
            {
                'type': 'user',
                'content': 'Hello',
                'ts': 1000000,
                'session_id': 'test_session',
                'metadata': {},
            },
        ])
        result = cl.get_entries_for_llm()
        assert len(result) == 1
        assert '_wrapped' not in result[0]

    def test_wrapped_false_does_not_produce_flag(self):
        """wrapped=False is falsy, should not set _wrapped flag."""
        cl = self._make_chatlog([
            {
                'type': 'user',
                'content': 'Hello',
                'ts': 1000000,
                'session_id': 'test_session',
                'metadata': {'wrapped': False},
            },
        ])
        result = cl.get_entries_for_llm()
        assert len(result) == 1
        assert '_wrapped' not in result[0]


# ---------------------------------------------------------------------------
# Tests: _build_static_prompt protocol section
# ---------------------------------------------------------------------------

class TestBuildStaticPrompt:
    """Protocol section exists in every agent's system prompt."""

    def test_protocol_section_present(self):
        """_build_static_prompt includes Message Wrapper Protocol."""
        agent = {
            'id': 'test_agent',
            'is_super': False,
            'sandbox_enabled': False,
        }
        result = _build_static_prompt(agent)
        assert 'Message Wrapper Protocol' in result
        assert 'Scan the message for any new preference' in result
        assert 'remember()' in result
        assert 'notes.md' in result

    def test_super_agent_also_gets_protocol(self):
        """Super agents also get the protocol section."""
        agent = {
            'id': 'siwa',
            'is_super': True,
            'sandbox_enabled': True,
        }
        result = _build_static_prompt(agent)
        assert 'Message Wrapper Protocol' in result
