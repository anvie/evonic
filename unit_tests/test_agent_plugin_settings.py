"""Per-agent plugin settings: the value types a plugin can declare in agent_settings."""
import pytest

from backend.plugin_manager import plugin_manager

SCHEMA = [
    {"name": "notes", "label": "Notes", "type": "textarea", "default": "", "rows": 4},
    {"name": "mode", "label": "Mode", "type": "select", "default": "a",
     "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}]},
    {"name": "token", "label": "Token", "type": "password", "default": ""},
    {"name": "limit", "label": "Limit", "type": "number", "default": 3},
    {"name": "on", "label": "On", "type": "boolean", "default": False},
]


@pytest.fixture
def declared(monkeypatch):
    monkeypatch.setattr(plugin_manager, "get_agent_settings_schema", lambda plugin_id: SCHEMA)
    return "test-plugin", "agent-for-plugin-settings-test"


def test_textarea_values_round_trip_with_newlines_quotes_and_markup(declared):
    plugin_id, agent_id = declared
    text = 'Line one with "quotes" and \'apostrophes\'\n\n- bullet <b>bold</b> & more\nlast line'
    assert plugin_manager.set_agent_plugin_settings(plugin_id, agent_id, {"notes": text}) == {"success": True}
    assert plugin_manager.get_agent_plugin_settings(plugin_id, agent_id)["notes"] == text


def test_defaults_are_returned_until_a_value_is_saved(declared):
    plugin_id, agent_id = declared
    values = plugin_manager.get_agent_plugin_settings(plugin_id, "agent-with-nothing-saved")
    assert values == {"notes": "", "mode": "a", "token": "", "limit": 3, "on": False}


def test_select_accepts_only_declared_options(declared):
    plugin_id, agent_id = declared
    assert plugin_manager.set_agent_plugin_settings(plugin_id, agent_id, {"mode": "b"}) == {"success": True}
    assert plugin_manager.get_agent_plugin_settings(plugin_id, agent_id)["mode"] == "b"
    result = plugin_manager.set_agent_plugin_settings(plugin_id, agent_id, {"mode": "c"})
    assert "must be one of a, b" in result["error"]
    assert plugin_manager.get_agent_plugin_settings(plugin_id, agent_id)["mode"] == "b"


def test_number_and_boolean_values_keep_their_types(declared):
    plugin_id, agent_id = declared
    plugin_manager.set_agent_plugin_settings(plugin_id, agent_id, {"limit": "7", "on": True, "token": "s3cret"})
    values = plugin_manager.get_agent_plugin_settings(plugin_id, agent_id)
    assert values["limit"] == 7 and values["on"] is True and values["token"] == "s3cret"
