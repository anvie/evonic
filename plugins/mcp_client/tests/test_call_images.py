"""
Tests for images in mcp_call results.

An MCP tool may answer with image blocks next to its text (a screenshot, a
chart). mcp_call hands them on as the tool result's `images`, which the runtime
(`backend.agent_runtime.tool_images`) saves as attachments for the model to see.
The tool is loaded in isolation against the real `_mcp_lib`, with a fake server
and a fake `models.db`.
"""

from __future__ import annotations

import base64
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "backend" / "tools"
_PKG = "mcp_call_under_test"

_JPEG = base64.b64encode(b"\xff\xd8\xff\xe0 not really a picture").decode()


class FakeServer:
    def __init__(self, result: dict):
        self._result = result

    def call_tool(self, tool, arguments, timeout, startup_timeout=0):
        return self._result


class FakeManager:
    def __init__(self, result: dict):
        self._result = result

    def get(self, name: str) -> FakeServer:
        return FakeServer(self._result)

    def get_timeouts(self):
        return (30, 10)


def _load(name: str):
    full = f"{_PKG}.{name}"
    spec = importlib.util.spec_from_file_location(full, _TOOLS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def call(monkeypatch):
    """mcp_call.execute against a server that answers with `result`."""
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(_TOOLS_DIR)]
    monkeypatch.setitem(sys.modules, _PKG, pkg)
    db_mod = types.ModuleType("models.db")
    db_mod.db = types.SimpleNamespace(get_setting=lambda key: "1")
    monkeypatch.setitem(sys.modules, "models.db", db_mod)
    lib = _load("_mcp_lib")
    monkeypatch.setitem(sys.modules, f"{_PKG}._mcp_lib", lib)
    tool = _load("mcp_call")
    monkeypatch.setitem(sys.modules, f"{_PKG}.mcp_call", tool)

    def run(result: dict) -> dict:
        monkeypatch.setattr(lib, "mcp_manager", FakeManager(result))
        return tool.execute({}, {"server": "demo-deck", "tool": "slide_view", "arguments": {"slide_id": "s1"}})

    return run


def test_an_image_block_becomes_a_tool_image(call):
    out = call({"content": [
        {"type": "text", "text": '{"ok":true,"width":1280}'},
        {"type": "image", "data": _JPEG, "mimeType": "image/jpeg"},
    ], "isError": False})

    assert out["images"] == [{"mime": "image/jpeg", "data": _JPEG, "label": "demo-deck slide_view"}]
    assert '{"ok":true,"width":1280}' in out["result"]
    assert "see images" in out["result"] and "omitted" not in out["result"]
    assert _JPEG not in out["result"], "the text stays text: no base64 in it"


def test_a_text_only_result_has_no_images(call):
    out = call({"content": [{"type": "text", "text": "done"}], "isError": False})

    assert out == {"result": "done", "server": "demo-deck", "tool": "slide_view"}


def test_other_content_is_still_named_as_omitted(call):
    out = call({"content": [{"type": "audio", "data": "AAAA", "mimeType": "audio/wav"}, {"type": "image"}], "isError": False})

    assert out["result"] == "[audio content omitted]\n[image content omitted]"
    assert "images" not in out


def test_the_runtime_stores_what_mcp_call_returns(call, tmp_path, monkeypatch):
    from backend.agent_runtime import tool_images

    monkeypatch.setattr(tool_images, "_attachment_dir", lambda agent_id, session_id: str(tmp_path))
    saved = []
    db = types.SimpleNamespace(save_attachment=lambda **kw: saved.append(kw) or 7)
    out = call({"content": [{"type": "image", "data": _JPEG, "mimeType": "image/jpeg"}], "isError": False})

    stored = tool_images.store(out, "agent", "session", "mcp_call", db)

    assert stored["images"][0][tool_images.REF_KEY] == 7 and stored["images"][0]["label"] == "demo-deck slide_view"
    assert saved[0]["mime_type"] == "image/jpeg" and Path(saved[0]["file_path"]).read_bytes() == base64.b64decode(_JPEG)
