"""Agent PDF toggle and managed-tool behavior."""

from app import app
from backend.agent_runtime.context import build_tools
from models.db import db


def _client():
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True
    return client


def test_pdf_enabled_defaults_on_and_exposes_analyze_pdf():
    db.create_agent({'id': 'pdf_default_agent', 'name': 'PDF', 'system_prompt': ''})
    agent = db.get_agent('pdf_default_agent')

    assert agent['pdf_enabled'] == 1
    tool_names = {tool['function']['name'] for tool in build_tools(agent)}
    assert 'analyze_pdf' in tool_names


def test_pdf_toggle_manages_tool_assignment_and_exposure():
    db.create_agent({'id': 'pdf_toggle_agent', 'name': 'PDF', 'system_prompt': ''})
    db.add_agent_tool('pdf_toggle_agent', 'analyze_pdf')
    client = _client()

    disabled = client.put('/api/agents/pdf_toggle_agent', json={'pdf_enabled': 0})
    assert disabled.status_code == 200
    assert db.get_agent('pdf_toggle_agent')['pdf_enabled'] == 0
    assert 'analyze_pdf' not in db.get_agent_tools('pdf_toggle_agent')
    assert 'analyze_pdf' not in {
        tool['function']['name'] for tool in build_tools(db.get_agent('pdf_toggle_agent'))
    }

    enabled = client.put('/api/agents/pdf_toggle_agent', json={'pdf_enabled': 1})
    assert enabled.status_code == 200
    assert 'analyze_pdf' in db.get_agent_tools('pdf_toggle_agent')
    assert 'analyze_pdf' in {
        tool['function']['name'] for tool in build_tools(db.get_agent('pdf_toggle_agent'))
    }


def test_tools_api_cannot_override_pdf_toggle_lock():
    db.create_agent({
        'id': 'pdf_lock_agent', 'name': 'PDF', 'system_prompt': '',
        'pdf_enabled': False,
    })
    client = _client()

    disabled = client.put(
        '/api/agents/pdf_lock_agent/tools', json={'tools': ['analyze_pdf']}
    ).get_json()
    assert 'analyze_pdf' not in disabled['tools']

    client.put('/api/agents/pdf_lock_agent', json={'pdf_enabled': 1})
    enabled = client.put(
        '/api/agents/pdf_lock_agent/tools', json={'tools': []}
    ).get_json()
    assert 'analyze_pdf' in enabled['tools']
