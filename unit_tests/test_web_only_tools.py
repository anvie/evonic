"""Tools declared web_only must not be advertised on external channels."""

from backend.agent_runtime import context
from backend.agent_runtime.context import build_tools

TOOL_ID = 'plugin:ask_user:ask_user_question'


def _setup(monkeypatch, web_only):
    tool_def = {
        'id': TOOL_ID,
        'web_only': web_only,
        'function': {'name': 'ask_user_question', 'description': 'd', 'parameters': {'type': 'object'}},
    }
    monkeypatch.setattr(context.db, 'get_agent_tools', lambda agent_id: [TOOL_ID])
    monkeypatch.setattr(context.db, 'get_agent_skills', lambda agent_id: [])
    monkeypatch.setattr(context.tool_registry, 'get_all_tool_defs', lambda: [tool_def])
    return {
        'id': 'web_only_agent',
        'is_super': False,
        'builtin_tools_enabled': False,
        'agent_messaging_enabled': False,
        'vision_enabled': False,
    }


def _names(agent, **kwargs):
    return {t['function']['name'] for t in build_tools(agent, **kwargs)}


def test_web_only_tool_is_listed_in_web_chat(monkeypatch):
    agent = _setup(monkeypatch, web_only=True)
    assert 'ask_user_question' in _names(agent)
    assert 'ask_user_question' in _names(agent, channel_id=None)


def test_web_only_tool_is_hidden_on_external_channel(monkeypatch):
    agent = _setup(monkeypatch, web_only=True)
    assert 'ask_user_question' not in _names(agent, channel_id='whatsapp-1')


def test_regular_tool_is_kept_on_external_channel(monkeypatch):
    agent = _setup(monkeypatch, web_only=False)
    assert 'ask_user_question' in _names(agent, channel_id='whatsapp-1')
