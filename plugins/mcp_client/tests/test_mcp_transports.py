"""Tests for _mcp_lib: the Streamable HTTP transport against a local stub server, the stdio transport against a stub
subprocess, and MCP_SERVERS validation."""

import importlib.util
import json
import os
import sys
import textwrap
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend", "tools", "_mcp_lib.py")


@pytest.fixture
def lib():
    spec = importlib.util.spec_from_file_location("mcp_lib_under_test", _LIB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Stub:
    """A minimal MCP server over HTTP. `sse` answers requests as event streams (with a ping and a notification
    before the response); `expire()` forgets the session so the next request gets 404."""

    TOKEN = "Bearer secret"

    def __init__(self, sse=False):
        self.sse = sse
        self.session = None
        self.sessions_made = 0
        self.received = []  # (method or reply id, headers)
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/json", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.received.append((msg.get("method", msg.get("id")), {k.lower(): v for k, v in self.headers.items()}))
                if self.headers.get("Authorization") != stub.TOKEN:
                    return self._send(401, b'{"error": "bad token"}')
                method = msg.get("method")
                extra = {}
                if method == "initialize":
                    stub.sessions_made += 1
                    stub.session = f"s{stub.sessions_made}"
                    extra["Mcp-Session-Id"] = stub.session
                elif self.headers.get("Mcp-Session-Id") != stub.session:
                    return self._send(404, b"")
                if "id" not in msg or "method" not in msg:  # a notification, or our reply to the ping
                    return self._send(202)
                result = {
                    "initialize": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                   "serverInfo": {"name": "stub", "version": "1"}},
                    "tools/list": {"tools": [{"name": "echo", "description": "Echo", "inputSchema": {"type": "object"}}]},
                    "tools/call": {"content": [{"type": "text", "text": json.dumps(msg.get("params", {}).get("arguments"))}],
                                   "isError": False},
                }.get(method)
                reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result} if result is not None else \
                    {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "nope"}}
                if not stub.sse or method == "initialize":
                    return self._send(200, json.dumps(reply).encode(), extra=extra)
                events = [
                    {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
                    {"jsonrpc": "2.0", "id": "srv-1", "method": "ping"},
                    reply,
                ]
                body = "".join(f"event: message\ndata: {json.dumps(e)}\n\n" for e in events).encode()
                self._send(200, body, ctype="text/event-stream", extra=extra)

            def do_DELETE(self):
                stub.received.append(("DELETE", {k.lower(): v for k, v in self.headers.items()}))
                self._send(200)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def expire(self):
        self.session = "gone"

    def methods(self):
        return [m for m, _ in self.received]


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.server.shutdown()


def http_server(lib, stub, token=Stub.TOKEN):
    return lib.MCPHttpServer("stub", {"url": stub.url, "headers": {"Authorization": token}, "enabled": True})


def test_http_lists_and_calls_tools_in_one_session(lib, stub):
    srv = http_server(lib, stub)
    tools = srv.list_tools(timeout=5)
    assert [t["name"] for t in tools] == ["echo"]
    result = srv.call_tool("echo", {"x": 1}, timeout=5)
    assert result == {"content": [{"type": "text", "text": '{"x": 1}'}], "isError": False}

    assert stub.methods() == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    headers = stub.received[-1][1]
    assert headers["mcp-session-id"] == "s1"
    assert headers["mcp-protocol-version"] == "2025-06-18"
    assert "text/event-stream" in headers["accept"]
    srv.stop()
    assert stub.methods()[-1] == "DELETE"


def test_http_reads_event_streams_and_answers_pings(lib):
    stub = Stub(sse=True)
    try:
        srv = http_server(lib, stub)
        result = srv.call_tool("echo", {"y": 2}, timeout=5)
        assert result["content"][0]["text"] == '{"y": 2}'
        assert "srv-1" in stub.methods(), "the server's ping was answered"
    finally:
        stub.server.shutdown()


def test_http_reconnects_when_the_session_expires(lib, stub):
    srv = http_server(lib, stub)
    srv.list_tools(timeout=5)
    stub.expire()
    result = srv.call_tool("echo", {"z": 3}, timeout=5)
    assert result["content"][0]["text"] == '{"z": 3}'
    assert stub.sessions_made == 2
    assert stub.methods().count("tools/call") == 2, "re-sent once in the new session"


def test_http_errors_name_the_status(lib, stub):
    srv = http_server(lib, stub, token="Bearer wrong")
    with pytest.raises(lib.MCPError, match="HTTP 401.*bad token"):
        srv.list_tools(timeout=5)
    gone = lib.MCPHttpServer("gone", {"url": "http://127.0.0.1:9/mcp", "headers": {}, "enabled": True})
    with pytest.raises(lib.MCPError, match="initialize failed"):
        gone.list_tools(timeout=2)


def test_http_protocol_errors_are_raised(lib, stub):
    srv = http_server(lib, stub)
    srv.ensure_started(5)
    with pytest.raises(lib.MCPError, match="nope"):
        srv._request("resources/list", {}, 5)


STDIO_SERVER = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        result = {"initialize": {"protocolVersion": "2025-06-18", "capabilities": {}},
                  "tools/list": {"tools": [{"name": "stdio-echo"}]}}.get(msg["method"], {})
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
""")


def test_stdio_still_works(lib):
    srv = lib.MCPServerProcess("stdio", {"command": sys.executable, "args": ["-c", STDIO_SERVER], "env": {},
                                         "cwd": None, "enabled": True})
    try:
        assert [t["name"] for t in srv.list_tools(timeout=10)] == ["stdio-echo"]
    finally:
        srv.stop()


def test_configs_take_a_command_or_a_url(lib, monkeypatch):
    raw = {}
    fake = types.SimpleNamespace(plugin_manager=types.SimpleNamespace(get_plugin_config=lambda _: {"MCP_SERVERS": json.dumps(raw)}))
    monkeypatch.setitem(sys.modules, "backend.plugin_manager", fake)
    manager = lib.MCPManager()

    raw.update({"remote": {"url": " https://deck.example/mcp ", "headers": {"Authorization": "Bearer t"}},
                "local": {"command": "claude", "args": ["mcp", "serve"]}})
    configs = manager._load_server_configs()
    assert configs["remote"] == {"url": "https://deck.example/mcp", "headers": {"Authorization": "Bearer t"}, "enabled": True}
    assert configs["local"]["command"] == "claude"
    assert isinstance(manager.get("remote"), lib.MCPHttpServer)
    assert isinstance(manager.get("local"), lib.MCPServerProcess)

    for bad, why in [({"url": "ftp://x"}, "http"), ({"url": "https://x", "command": "c"}, "not both"),
                     ({"url": "https://x", "headers": "Bearer t"}, "headers"), ({}, "'url' is required")]:
        raw.clear()
        raw["s"] = bad
        with pytest.raises(lib.MCPError, match=why):
            manager._load_server_configs()


def test_list_servers_reports_enabled_servers_without_secrets(lib, monkeypatch):
    raw = {"remote": {"url": "https://deck.example/mcp", "headers": {"Authorization": "Bearer t"}},
           "local": {"command": "claude", "args": ["mcp", "serve"]},
           "off": {"command": "x", "enabled": False}}
    fake = types.SimpleNamespace(plugin_manager=types.SimpleNamespace(get_plugin_config=lambda _: {"MCP_SERVERS": json.dumps(raw)}))
    monkeypatch.setitem(sys.modules, "backend.plugin_manager", fake)
    assert lib.MCPManager().list_servers() == [{"name": "local", "transport": "stdio"},
                                               {"name": "remote", "transport": "http"}]
