"""Regression coverage for the agent tool-assignment API."""

from app import app
from models.db import db


def _create_agent(agent_id: str, *, builtin_tools_enabled: bool) -> None:
    db.create_agent({
        'id': agent_id,
        'name': agent_id,
        'system_prompt': '',
        'builtin_tools_enabled': builtin_tools_enabled,
        'agent_messaging_enabled': True,
    })


def test_disabled_builtins_do_not_reappear_as_assigned_messaging_tools():
    """Refreshes must not re-add auto-loaded tools after built-ins are disabled."""
    _create_agent('messaging_disabled', builtin_tools_enabled=False)

    with app.test_client() as client:
        with client.session_transaction() as session:
            session['authenticated'] = True
        response = client.get('/api/agents/messaging_disabled/tools')

    assert response.status_code == 200
    assert response.get_json()['tools'] == []


def test_enabled_builtins_include_auto_loaded_messaging_tools():
    """The existing auto-loaded behavior remains intact while built-ins are enabled."""
    _create_agent('messaging_enabled', builtin_tools_enabled=True)

    with app.test_client() as client:
        with client.session_transaction() as session:
            session['authenticated'] = True
        response = client.get('/api/agents/messaging_enabled/tools')

    assert response.status_code == 200
    assert set(response.get_json()['tools']) == {
        'send_agent_message',
        'escalate_to_user',
        'resolve_agent_approval',
        'list_sessions',
        'send_channel_message',
    }
