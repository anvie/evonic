"""
Tests for the compact mcp_list_tools listing.

The tool is loaded in isolation: a fake `_mcp_lib` supplies the server/tool
fixture and a fake `models.db` reports the plugin as enabled, so nothing here
touches a real MCP server or the platform database.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "backend" / "tools"
_PKG = "mcp_tools_under_test"

_TOOL_NAMES = [
    "deck_list", "deck_create", "deck_get", "deck_delete",
    "deck_set_theme", "deck_set_transition", "deck_set_custom_theme",
    "theme_list", "theme_create", "theme_update",
    "slide_layouts", "slide_create", "slide_update", "slide_delete",
    "slide_move", "slide_set_title",
    "illustration_search", "illustration_get", "illustration_fork",
    "illustration_update", "illustration_brief", "illustration_create",
    "scene_guide", "scene_preview", "scene_render", "scene_status",
    "photo_view",
    "master_list", "master_get", "master_update",
    "decoration_list", "decoration_create", "decoration_update",
    "decoration_delete",
]

_DESCRIPTION = (
    "Create, update or inspect a presentation artefact on the deck server. "
) * 6


def _schema(index: int) -> dict:
    props = {
        "presentation_id": {"type": "string", "description": "Presentation id"},
        "name": {"type": "string", "description": "Display name"},
        "limit": {"type": "integer"},
    }
    if index % 3 == 0:
        props["mode"] = {"type": "string", "enum": ["fast", "precise", "draft"]}
    return {
        "type": "object",
        "properties": props,
        "required": ["presentation_id"],
    }


def _tools(count: int = len(_TOOL_NAMES)) -> list[dict]:
    return [
        {
            "name": name,
            "description": _DESCRIPTION,
            "inputSchema": _schema(i),
        }
        for i, name in enumerate(_TOOL_NAMES[:count])
    ]


class FakeMCPError(Exception):
    """Stand-in for _mcp_lib.MCPError."""


class FakeServer:
    def __init__(self, tools: list[dict]):
        self._tools = tools

    def list_tools(self, timeout, startup_timeout=0):
        return self._tools


class FakeManager:
    def __init__(self, servers: dict[str, list[dict]] | None = None):
        self.servers = {"nuwaira-deck": _tools()} if servers is None else servers

    def server_names(self) -> list[str]:
        return list(self.servers)

    def get(self, name: str) -> FakeServer:
        if name not in self.servers:
            raise FakeMCPError(f"unknown MCP server: {name}")
        return FakeServer(self.servers[name])

    def get_timeouts(self):
        return (30, 10)


@pytest.fixture
def tool(monkeypatch):
    """Load mcp_list_tools.py against a fake _mcp_lib and a fake database."""
    lib = types.ModuleType(f"{_PKG}._mcp_lib")
    lib.MCPError = FakeMCPError
    lib.mcp_manager = FakeManager()

    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(_TOOLS_DIR)]
    monkeypatch.setitem(sys.modules, _PKG, pkg)
    monkeypatch.setitem(sys.modules, f"{_PKG}._mcp_lib", lib)

    db_mod = types.ModuleType("models.db")
    db_mod.db = types.SimpleNamespace(get_setting=lambda key: "1")
    monkeypatch.setitem(sys.modules, "models.db", db_mod)

    name = f"{_PKG}.mcp_list_tools"
    spec = importlib.util.spec_from_file_location(name, _TOOLS_DIR / "mcp_list_tools.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)

    module._fake_lib = lib  # tests swap the manager through this
    return module


def _size(payload) -> int:
    return len(json.dumps(payload))


# ------------------------------------------------------------------ default


class TestDefaultListing:
    def test_is_compact_but_names_every_tool(self, tool):
        payload = tool.execute({}, {"server": "nuwaira-deck"})

        assert payload["server"] == "nuwaira-deck"
        assert payload["tool_count"] == len(_TOOL_NAMES)
        assert payload["shown"] == len(_TOOL_NAMES)
        assert [t["name"] for t in payload["tools"]] == _TOOL_NAMES
        # Stays under the size at which the RTK filter would step in.
        assert _size(payload) < 12000
        assert payload["hint"]

    def test_omits_raw_schemas(self, tool):
        payload = tool.execute({}, {"server": "nuwaira-deck"})
        assert all("input_schema" not in t for t in payload["tools"])

    def test_renders_argument_signatures(self, tool):
        payload = tool.execute({}, {"server": "nuwaira-deck"})
        by_name = {t["name"]: t for t in payload["tools"]}

        args = by_name["deck_list"]["args"]
        assert "presentation_id:string!" in args      # required
        assert "name:string?" in args                 # optional
        assert "limit:integer?" in args

    def test_renders_enum_values(self, tool):
        payload = tool.execute({}, {"server": "nuwaira-deck"})
        by_name = {t["name"]: t for t in payload["tools"]}
        assert "mode:string[fast|precise|draft]?" in by_name["deck_list"]["args"]

    def test_trims_long_descriptions(self, tool):
        payload = tool.execute({}, {"server": "nuwaira-deck"})
        for entry in payload["tools"]:
            assert len(entry["description"]) <= 200

    def test_all_servers_envelope(self, tool):
        tool._fake_lib.mcp_manager = FakeManager(
            {"nuwaira-deck": _tools(3), "claude-code": []}
        )
        payload = tool.execute({}, {})
        assert len(payload["servers"]) == 2
        assert payload["servers"][1] == {
            "server": "claude-code", "tool_count": 0, "shown": 0,
            "tools": [], "hint": payload["servers"][1]["hint"],
        }


# -------------------------------------------------------------- names_only


class TestNamesOnly:
    def test_returns_names_without_descriptions(self, tool):
        payload = tool.execute({}, {"server": "nuwaira-deck", "names_only": True})

        assert payload["tool_count"] == len(_TOOL_NAMES)
        assert payload["tools"] == _TOOL_NAMES
        assert _size(payload) < 2000

    def test_accepts_truthy_strings(self, tool):
        payload = tool.execute({}, {"server": "nuwaira-deck", "names_only": "true"})
        assert payload["tools"][0] == _TOOL_NAMES[0]

    def test_combines_with_filter(self, tool):
        payload = tool.execute(
            {}, {"server": "nuwaira-deck", "names_only": True, "filter": "illustration"}
        )
        assert payload["tool_count"] == len(_TOOL_NAMES)
        assert all(n.startswith("illustration") for n in payload["tools"])


# ------------------------------------------------------------------ filter


class TestFilter:
    def test_substring_match_includes_full_schema(self, tool):
        payload = tool.execute({}, {"server": "nuwaira-deck", "filter": "illustration"})

        names = [t["name"] for t in payload["tools"]]
        assert names == [n for n in _TOOL_NAMES if "illustration" in n]
        assert payload["shown"] == len(names)
        assert payload["shown"] < payload["tool_count"]
        assert all("input_schema" in t for t in payload["tools"])
        assert "note" in payload

    def test_matches_description_text_too(self, tool):
        payload = tool.execute({}, {"server": "nuwaira-deck", "filter": "presentation artefact"})
        assert payload["shown"] == len(_TOOL_NAMES)

    def test_regex_match(self, tool):
        payload = tool.execute(
            {}, {"server": "nuwaira-deck", "filter": "^deck_(list|create)$"}
        )
        assert [t["name"] for t in payload["tools"]] == ["deck_list", "deck_create"]

    def test_is_case_insensitive(self, tool):
        payload = tool.execute({}, {"server": "nuwaira-deck", "filter": "ILLUSTRATION"})
        assert payload["shown"] == 6

    def test_no_match_returns_empty_list(self, tool):
        payload = tool.execute({}, {"server": "nuwaira-deck", "filter": "nothing-here"})
        assert payload["tools"] == []
        assert payload["shown"] == 0
        assert payload["tool_count"] == len(_TOOL_NAMES)

    def test_filtered_subset_is_small(self, tool):
        payload = tool.execute({}, {"server": "nuwaira-deck", "filter": "illustration"})
        assert _size(payload) < 8000


# ---------------------------------------------------------- include_schema


class TestIncludeSchema:
    def test_every_tool_carries_its_schema_for_a_small_server(self, tool):
        tool._fake_lib.mcp_manager = FakeManager({"small": _tools(3)})
        payload = tool.execute({}, {"server": "small", "include_schema": True})
        assert all("input_schema" in t for t in payload["tools"])

    def test_oversized_payload_drops_schemas_and_explains(self, tool):
        payload = tool.execute({}, {"server": "nuwaira-deck", "include_schema": True})
        assert _size(payload) < 25000
        assert all("input_schema" not in t for t in payload["tools"])
        assert "filter=" in payload["note"]


# ------------------------------------------------------------------ errors


class TestErrors:
    def test_disabled_plugin(self, tool, monkeypatch):
        db_mod = types.ModuleType("models.db")
        db_mod.db = types.SimpleNamespace(get_setting=lambda key: "0")
        monkeypatch.setitem(sys.modules, "models.db", db_mod)
        assert "disabled" in tool.execute({}, {"server": "nuwaira-deck"})["error"]

    def test_unknown_server_reports_error(self, tool):
        payload = tool.execute({}, {"server": "nope"})
        assert payload["server"] == "nope"
        assert "unknown MCP server" in payload["error"]

    def test_no_servers_configured(self, tool):
        tool._fake_lib.mcp_manager = FakeManager({})
        assert "No MCP servers configured" in tool.execute({}, {})["error"]


# ------------------------------------------------------------- size guard


class TestSizeGuard:
    def test_huge_toolset_still_fits_under_the_soft_cap(self, tool):
        """A 150-tool server must still produce a listing the runtime keeps whole."""
        big = [dict(t, name=f"{t['name']}_{i}") for i in range(5) for t in _tools(30)]
        tool._fake_lib.mcp_manager = FakeManager({"big": big})
        payload = tool.execute({}, {"server": "big"})

        assert payload["tool_count"] == len(big)
        assert _size(payload) <= tool._SOFT_CAP
        # Names — the one thing the agent cannot work without — all survive.
        assert [t["name"] for t in payload["tools"]] == [t["name"] for t in big]
