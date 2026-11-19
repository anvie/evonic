"""Tests for implicit tool assignment (monitor wherever bash is assigned).

Regression guard for the drift that made the monitor tool visible to an agent
with bash while the authorization guard rejected every call with
``blocked_by: authorization`` (Joko session joko-8bc8d7b3, 2026-11-19).
"""

import pytest

from backend.agent_runtime.tool_assignment import (
    apply_implicit_tools,
    implicit_tool_ids,
)
from backend.tools.registry import ToolRegistry


class TestApplyImplicitTools:
    def test_adds_monitor_for_bash_list(self):
        ids = ['bash', 'read_file']
        assert apply_implicit_tools(ids) is ids
        assert 'monitor' in ids

    def test_adds_monitor_for_bash_set(self):
        ids = {'bash', 'read_file'}
        apply_implicit_tools(ids)
        assert 'monitor' in ids

    def test_idempotent(self):
        ids = ['bash']
        apply_implicit_tools(ids)
        apply_implicit_tools(ids)
        assert ids.count('monitor') == 1

    def test_no_monitor_without_bash(self):
        ids = ['read_file', 'write_file']
        apply_implicit_tools(ids)
        assert 'monitor' not in ids
        assert implicit_tool_ids(ids) == set()

    def test_unrelated_container_untouched(self):
        ids = set()
        apply_implicit_tools(ids)
        assert ids == set()


class TestGuardAgreesWithHelper:
    """The authorization guard must not block a tool the helper just added."""

    @pytest.fixture
    def registry(self):
        return ToolRegistry()

    def _context(self, tool_ids):
        return {
            'agent_id': 'test-agent',
            'assigned_tool_ids': list(tool_ids),
            'is_super': False,
            'session_id': 'test-session',
        }

    def test_monitor_allowed_after_helper(self, registry):
        ids = ['bash']
        apply_implicit_tools(ids)
        executor = registry.get_real_executor(self._context(ids))
        result = executor('monitor', {'action': 'bogus'})
        assert result.get('blocked_by') != 'authorization'

    def test_monitor_blocked_without_bash(self, registry):
        ids = ['read_file']
        apply_implicit_tools(ids)
        executor = registry.get_real_executor(self._context(ids))
        result = executor('monitor', {'action': 'bogus'})
        assert result.get('blocked_by') == 'authorization'
