"""Regression tests for assignment-aware messaging tool definitions."""

from backend.agent_runtime.context import build_tools
from backend.agent_runtime import context


def _tool_names(agent):
    return {
        tool['function']['name']
        for tool in build_tools(agent)
        if tool.get('function', {}).get('name')
    }


def test_messaging_tools_require_explicit_assignment(monkeypatch):
    """Do not advertise messaging definitions that runtime would reject."""
    monkeypatch.setattr(
        context.db,
        'get_agent_tools',
        lambda agent_id: ['send_agent_message', 'send_channel_message'],
    )
    monkeypatch.setattr(context.db, 'get_agent_skills', lambda agent_id: [])

    agent = {
        'id': 'messaging_agent',
        'is_super': False,
        'builtin_tools_enabled': False,
        'agent_messaging_enabled': True,
        'vision_enabled': False,
    }

    names = _tool_names(agent)

    assert 'send_agent_message' in names
    assert 'send_channel_message' in names
    assert 'list_sessions' not in names
    assert 'escalate_to_user' not in names
    assert 'resolve_agent_approval' not in names


def test_enabled_agent_receives_send_agent_message_without_assignment(monkeypatch):
    """The agent toggle auto-enables the core inter-agent message tool."""
    monkeypatch.setattr(context.db, 'get_agent_tools', lambda agent_id: [])
    monkeypatch.setattr(context.db, 'get_agent_skills', lambda agent_id: [])

    names = _tool_names({
        'id': 'enabled_agent',
        'is_super': False,
        'builtin_tools_enabled': False,
        'agent_messaging_enabled': True,
        'vision_enabled': False,
    })

    assert 'send_agent_message' in names


def test_super_agent_receives_all_messaging_tools(monkeypatch):
    """Super agents intentionally retain the complete messaging tool set."""
    monkeypatch.setattr(context.db, 'get_agent_tools', lambda agent_id: [])
    monkeypatch.setattr(context.db, 'get_agent_skills', lambda agent_id: [])

    names = _tool_names({
        'id': 'super_agent',
        'is_super': True,
        'builtin_tools_enabled': False,
        'agent_messaging_enabled': True,
        'vision_enabled': False,
    })

    assert {
        'send_agent_message',
        'escalate_to_user',
        'resolve_agent_approval',
        'list_sessions',
        'send_channel_message',
    } <= names
