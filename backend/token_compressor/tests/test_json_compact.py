"""
Tests for JSON-output compaction in RTK.

Covers:
  - filter_schema.py   -- parsing / validation of the json_* keys
  - filter_pipeline.py -- _stage_json_compact behaviour
  - builtin filter     -- mcp_list_tools.toml shrinks a real-sized listing
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _THIS_DIR.parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

from token_compressor.filter_pipeline import compress as pipeline_compress
from token_compressor.filter_schema import (
    FilterParseError,
    _compile_filter,
    load_filter,
    merge_filters,
)

_BUILTIN = _THIS_DIR.parent / "filters" / "builtin"
_MCP_FILTER = _BUILTIN / "mcp_list_tools.toml"


def _filter(**overrides):
    raw = {"command": "^mcp_list_tools"}
    raw.update(overrides)
    return _compile_filter(raw, source="<test>")


def _big_listing(tools: int = 34) -> str:
    """A listing roughly the size of the payload that caused the truncation."""
    payload = {
        "server": "demo-deck",
        "tool_count": tools,
        "shown": tools,
        "tools": [
            {
                "name": f"tool_{i}",
                "description": "Long description of the tool. " * 22,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "deck_id": {"type": "string", "description": "Deck id"},
                        "name": {"type": "string", "description": "Name"},
                        "mode": {"type": "string", "enum": ["a", "b", "c"]},
                    },
                    "required": ["deck_id"],
                },
            }
            for i in range(tools)
        ],
        "hint": "Compact listing",
    }
    return json.dumps(payload)


# ---------------------------------------------------------------- schema


class TestJsonSchemaParsing:
    def test_defaults_are_off(self):
        flt = _filter()
        assert flt.json_compact is False
        assert flt.json_compact_above is None
        assert flt.json_max_string is None
        assert flt.json_max_items is None
        assert flt.json_drop_keys == []
        assert flt.has_json_stage() is False

    def test_full_configuration_is_parsed(self):
        flt = _filter(
            json_compact=True,
            json_compact_above=5000,
            json_max_string=120,
            json_max_items=10,
            json_drop_keys=["input_schema", "debug"],
        )
        assert flt.json_compact is True
        assert flt.json_compact_above == 5000
        assert flt.json_max_string == 120
        assert flt.json_max_items == 10
        assert flt.json_drop_keys == ["input_schema", "debug"]

    def test_any_json_key_enables_the_stage_implicitly(self):
        assert _filter(json_max_items=5).has_json_stage() is True
        assert _filter(json_drop_keys=["x"]).has_json_stage() is True
        assert _filter(json_compact_above=1000).has_json_stage() is True

    def test_json_compact_must_be_bool(self):
        with pytest.raises(FilterParseError):
            _filter(json_compact="yes")

    def test_json_drop_keys_must_be_list_of_strings(self):
        with pytest.raises(FilterParseError):
            _filter(json_drop_keys="input_schema")
        with pytest.raises(FilterParseError):
            _filter(json_drop_keys=[1])

    def test_numeric_keys_reject_zero_and_non_int(self):
        with pytest.raises(FilterParseError):
            _filter(json_max_items=0)
        with pytest.raises(FilterParseError):
            _filter(json_max_string="240")

    def test_new_keys_are_not_reported_as_unknown(self, capsys):
        _filter(json_compact=True, json_drop_keys=["a"])
        assert "unknown key" not in capsys.readouterr().err


# -------------------------------------------------------------- pipeline


class TestJsonStage:
    def test_drops_keys_recursively(self):
        payload = {
            "tools": [{"name": "a", "input_schema": {"deep": {"input_schema": 1}}}],
            "input_schema": {"x": 1},
        }
        out = pipeline_compress(json.dumps(payload), _filter(json_drop_keys=["input_schema"]), 0)
        data = json.loads(out)
        assert "input_schema" not in data
        assert data["tools"] == [{"name": "a"}]

    def test_caps_long_strings(self):
        payload = {"description": "x" * 500}
        out = pipeline_compress(json.dumps(payload), _filter(json_max_string=50), 0)
        desc = json.loads(out)["description"]
        assert len(desc) <= 50
        assert desc.endswith("…")

    def test_caps_long_lists_with_marker(self):
        payload = {"tools": [{"name": f"t{i}"} for i in range(20)]}
        out = pipeline_compress(json.dumps(payload), _filter(json_max_items=5), 0)
        tools = json.loads(out)["tools"]
        assert tools[:5] == [{"name": f"t{i}"} for i in range(5)]
        assert tools[5] == "...[15 more items]"

    def test_compact_above_passes_small_payloads_through(self):
        payload = {"tools": [{"name": f"t{i}"} for i in range(3)]}
        raw = json.dumps(payload)
        out = pipeline_compress(raw, _filter(json_compact=True, json_max_items=1,
                                             json_compact_above=100000), 0)
        assert out == raw

    def test_compact_above_still_compacts_large_payloads(self):
        payload = {"tools": [{"name": f"t{i}"} for i in range(50)]}
        out = pipeline_compress(
            json.dumps(payload),
            _filter(json_compact=True, json_max_items=2, json_compact_above=10),
            0,
        )
        assert len(json.loads(out)["tools"]) == 3  # 2 kept + marker

    def test_non_json_output_passes_through(self):
        raw = "On branch main\n\tmodified: app.py\n"
        assert pipeline_compress(raw, _filter(json_compact=True, json_max_items=1), 0) == raw

    def test_scalar_json_is_left_alone(self):
        assert pipeline_compress("12345", _filter(json_compact=True), 0) == "12345"

    def test_keeps_original_when_nothing_can_be_saved(self):
        payload = {"a": 1, "b": [1, 2]}
        raw = json.dumps(payload)
        out = pipeline_compress(raw, _filter(json_compact=True, json_max_items=10), 0)
        assert out == raw

    def test_non_zero_exit_code_skips_compaction(self):
        payload = {"tools": [{"name": f"t{i}"} for i in range(10)]}
        raw = json.dumps(payload)
        out = pipeline_compress(raw, _filter(json_compact=True, json_max_items=1), 3)
        assert out == raw

    def test_compacted_output_is_valid_json(self):
        out = pipeline_compress(_big_listing(6), _filter(
            json_compact=True, json_max_string=80, json_max_items=3,
            json_drop_keys=["input_schema"]), 0)
        data = json.loads(out)
        assert "input_schema" not in data["tools"][0]


# ----------------------------------------------------------------- merge


class TestMergeFilters:
    def test_higher_priority_json_keys_win(self):
        base = _filter(json_compact=True, json_max_string=240, json_drop_keys=["a"])
        higher = _filter(json_max_items=7)
        merged = merge_filters(base, higher)
        assert merged.json_max_items == 7
        assert merged.json_max_string == 240
        assert merged.json_drop_keys == ["a"]

    def test_unset_fields_are_inherited(self):
        base = _filter(json_compact_above=1000, json_drop_keys=["k"])
        merged = merge_filters(base, _filter(description="override"))
        assert merged.json_compact_above == 1000
        assert merged.json_drop_keys == ["k"]


# --------------------------------------------------------------- builtin


class TestBuiltinMcpFilter:
    def test_loads_and_matches_the_tool_command(self):
        flt = load_filter(_MCP_FILTER)
        assert flt.matches_command("mcp_list_tools")
        assert flt.has_json_stage() is True

    def test_shrinks_an_oversized_listing_below_the_runtime_cap(self):
        from config import AGENT_MAX_TOOL_RESULT_CHARS

        raw = _big_listing()
        assert len(raw) > AGENT_MAX_TOOL_RESULT_CHARS
        out = pipeline_compress(raw, load_filter(_MCP_FILTER), 0)
        assert len(out) < AGENT_MAX_TOOL_RESULT_CHARS

        data = json.loads(out)
        assert len(data["tools"]) <= 60
        assert all("input_schema" not in t for t in data["tools"])
        # Names survive compaction — this is what the agent needs.
        assert data["tools"][0]["name"] == "tool_0"

    def test_small_listing_passes_through_untouched(self):
        payload = {"server": "s", "tool_count": 2, "tools": [
            {"name": "a", "description": "d", "args": "x:string!"},
            {"name": "b", "description": "d"},
        ]}
        raw = json.dumps(payload)
        assert pipeline_compress(raw, load_filter(_MCP_FILTER), 0) == raw
