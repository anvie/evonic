"""Plugin config save must reject malformed *_JSON values instead of storing them."""

import json

import pytest

from backend import plugin_lifecycle
from backend.plugin_manager import plugin_manager

PLUGIN_ID = "json-config-test-plugin"
SCHEMA = [
    {"name": "PRICING_JSON", "label": "Model Pricing (JSON)", "type": "textarea", "default": ""},
    {"name": "NOTE", "label": "Note", "type": "string", "default": ""},
]


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    (tmp_path / PLUGIN_ID).mkdir()
    monkeypatch.setattr(plugin_lifecycle, "PLUGINS_DIR", str(tmp_path))
    monkeypatch.setattr(plugin_manager, "get_plugin_variables", lambda plugin_id: SCHEMA)
    monkeypatch.setattr(plugin_manager, "reload_plugin", lambda plugin_id: {"success": True})
    monkeypatch.setattr(plugin_manager, "get_plugin_config",
                        lambda plugin_id: {v["name"]: v["default"] for v in SCHEMA})
    return PLUGIN_ID


def test_malformed_json_is_rejected_with_position(plugin, monkeypatch):
    stored = {}
    monkeypatch.setattr("models.db.db.set_setting",
                        lambda key, value: stored.setdefault(key, value))
    result = plugin_manager.set_plugin_config(
        plugin, {"PRICING_JSON": '{\n "a": {"in": 1, "out": 2}}\n}'})
    assert "Invalid JSON in Model Pricing (JSON)" in result["error"]
    assert "line" in result["error"] and "column" in result["error"]
    assert stored == {}


def test_non_object_json_is_rejected(plugin, monkeypatch):
    stored = {}
    monkeypatch.setattr("models.db.db.set_setting",
                        lambda key, value: stored.setdefault(key, value))
    result = plugin_manager.set_plugin_config(plugin, {"PRICING_JSON": '"just a string"'})
    assert "expected an object or array" in result["error"]
    assert stored == {}


def test_rejection_is_atomic_across_multiple_variables(plugin, monkeypatch):
    stored = {}
    monkeypatch.setattr("models.db.db.set_setting",
                        lambda key, value: stored.setdefault(key, value))
    result = plugin_manager.set_plugin_config(
        plugin, {"NOTE": "kept", "PRICING_JSON": "{broken"})
    assert "error" in result
    assert stored == {}


def test_valid_json_round_trips(plugin, monkeypatch):
    stored = {}
    monkeypatch.setattr("models.db.db.set_setting",
                        lambda key, value: stored.__setitem__(key, value))
    payload = json.dumps({"deepseek-v4-flash": {"in": 0.3, "out": 1.2}}, indent=4)
    result = plugin_manager.set_plugin_config(plugin, {"PRICING_JSON": payload})
    assert result["success"] is True
    assert stored[f"plugin_config:{plugin}:PRICING_JSON"] == payload


def test_empty_json_value_is_allowed(plugin, monkeypatch):
    stored = {}
    monkeypatch.setattr("models.db.db.set_setting",
                        lambda key, value: stored.__setitem__(key, value))
    assert plugin_manager.set_plugin_config(plugin, {"PRICING_JSON": "  "})["success"] is True
    assert stored[f"plugin_config:{plugin}:PRICING_JSON"] == "  "


def test_non_json_variables_are_not_validated(plugin, monkeypatch):
    stored = {}
    monkeypatch.setattr("models.db.db.set_setting",
                        lambda key, value: stored.__setitem__(key, value))
    assert plugin_manager.set_plugin_config(plugin, {"NOTE": "{not json"})["success"] is True
