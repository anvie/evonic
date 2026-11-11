"""PRICING_JSON handling: valid overrides apply, broken ones warn instead of vanishing."""

import json

import pytest

from backend.plugin_manager import plugin_manager
from plugins.token_monitor import pricing


def _config(raw):
    return lambda plugin_id: {"PRICING_JSON": raw}


def test_valid_override_prices_model(monkeypatch):
    monkeypatch.setattr(plugin_manager, "get_plugin_config",
                        _config(json.dumps({"deepseek-v4-flash": {"in": 0.3, "out": 1.2}})))
    assert pricing.cost("deepseek/deepseek-v4-flash", 1_000_000, 1_000_000) == 1.5


def test_broken_json_falls_back_to_defaults_and_warns(monkeypatch):
    warnings = []
    monkeypatch.setattr(plugin_manager, "get_plugin_config",
                        _config('{\n  "deepseek-v4-flash": {"in": 0.3, "out": 1.2}}\n}'))
    monkeypatch.setattr(plugin_manager, "add_log",
                        lambda plugin_id, level, message: warnings.append((level, message)))
    assert pricing.cost("deepseek-v4-flash", 1_000_000, 0) is None
    assert any("not valid JSON" in message for level, message in warnings if level == "warn")


def test_non_object_json_is_rejected_with_warning(monkeypatch):
    warnings = []
    monkeypatch.setattr(plugin_manager, "get_plugin_config", _config("[1, 2, 3]"))
    monkeypatch.setattr(plugin_manager, "add_log",
                        lambda plugin_id, level, message: warnings.append((level, message)))
    assert pricing.cost("deepseek-v4-flash", 1_000_000, 0) is None
    assert any("must be a JSON object" in message for level, message in warnings)


def test_entries_without_in_out_or_numeric_values_are_skipped(monkeypatch):
    warnings = []
    raw = json.dumps({
        "no-io": {"input": 1, "output": 2},
        "bad-numbers": {"in": "cheap", "out": 1.2},
        "chat-model": {"in": 2.0, "out": 4.0},
    })
    monkeypatch.setattr(plugin_manager, "get_plugin_config", _config(raw))
    monkeypatch.setattr(plugin_manager, "add_log",
                        lambda plugin_id, level, message: warnings.append((level, message)))

    table = pricing._load_pricing()
    assert "no-io" not in table and "bad-numbers" not in table
    assert table["chat-model"] == {"in": 2.0, "out": 4.0}
    assert len([w for w in warnings if "ignoring entry" in w[1]]) == 2


def test_empty_config_keeps_builtin_defaults(monkeypatch):
    monkeypatch.setattr(plugin_manager, "get_plugin_config", _config(""))
    assert pricing.cost("claude-sonnet-5-5", 1_000_000, 0) == 3.0


def test_config_read_failure_warns_instead_of_raising(monkeypatch):
    warnings = []

    def boom(plugin_id):
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(plugin_manager, "get_plugin_config", boom)
    monkeypatch.setattr(plugin_manager, "add_log",
                        lambda plugin_id, level, message: warnings.append((level, message)))
    assert pricing.cost("claude-sonnet-5-5", 1_000_000, 0) == 3.0
    assert any("could not read PRICING_JSON" in message for level, message in warnings)


def test_longest_key_wins(monkeypatch):
    monkeypatch.setattr(plugin_manager, "get_plugin_config", _config(""))
    assert pricing._match("claude-haiku-5-5", pricing._load_pricing()) == {
        "in": 0.80, "out": 4.0}
