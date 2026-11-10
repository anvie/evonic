"""
_mcp_lib.py — minimal MCP (Model Context Protocol) client over stdio and HTTP.

Spawns and manages long-lived MCP server subprocesses (e.g. `claude mcp serve`)
and speaks newline-delimited JSON-RPC 2.0 per the MCP stdio transport spec, or
talks to remote servers over the Streamable HTTP transport (a server entry with
a `url` instead of a `command`).
Used by the mcp_list_tools / mcp_call agent tools; servers are configured via
the `mcp_client` plugin's MCP_SERVERS variable.

The process-manager singleton survives both tool-entrypoint hot reloads and
plugin reloads: this helper is imported relatively by the tool modules, so it
lives in sys.modules as 'plugin_tools_mcp_client._mcp_lib' — a namespace the
plugin loader does not evict (only 'plugin_pkg_*' modules are). Editing this
file therefore requires an app restart to take effect.

Concurrency model: fully synchronous, one in-flight request per server,
guarded by a per-server lock. Processes (and HTTP sessions) are shared across
agents, started lazily on first use, and reaped at app exit via atexit.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import select
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PLUGIN_ID = "mcp_client"
PROTOCOL_VERSION = "2025-06-18"

_DEFAULT_CALL_TIMEOUT = 120.0
_DEFAULT_STARTUP_TIMEOUT = 60.0


class MCPError(Exception):
    """Raised for any MCP transport, protocol, or configuration failure."""

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.code = code


def content_to_text(content: List[dict]) -> str:
    """Flatten an MCP `content` array to plain text for the LLM."""
    parts = []
    for item in content or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
        else:
            parts.append(f"[{item.get('type', 'unknown')} content omitted]")
    return "\n".join(parts)


class _MCPServer:
    """The public API shared by both transports. Subclasses provide
    `_ensure_started_locked` and `_request`; all public methods serialize on
    `self._lock` — one request in flight per server.
    """

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.config_json = json.dumps(config, sort_keys=True)
        self._lock = threading.Lock()
        self._initialized = False
        self._next_id = 0

    # ── Public API ──

    def ensure_started(self, startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT) -> None:
        with self._lock:
            self._ensure_started_locked(startup_timeout)

    def list_tools(self, timeout: float,
                   startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT) -> List[dict]:
        """Full tool list from tools/list, following nextCursor pagination."""
        with self._lock:
            self._ensure_started_locked(startup_timeout)
            tools: List[dict] = []
            cursor = None
            while True:
                params: Dict[str, Any] = {"cursor": cursor} if cursor else {}
                result = self._request("tools/list", params, timeout)
                tools.extend(result.get("tools") or [])
                cursor = result.get("nextCursor")
                if not cursor:
                    return tools

    def call_tool(self, tool: str, arguments: dict, timeout: float,
                  startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT) -> dict:
        """Invoke tools/call; returns {"content": [...], "isError": bool}."""
        with self._lock:
            self._ensure_started_locked(startup_timeout)
            result = self._request(
                "tools/call", {"name": tool, "arguments": arguments}, timeout)
            return {
                "content": result.get("content") or [],
                "isError": bool(result.get("isError")),
            }

    def _ensure_started_locked(self, startup_timeout: float) -> None:
        raise NotImplementedError

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        raise NotImplementedError


class MCPServerProcess(_MCPServer):
    """One MCP server subprocess: spawn, handshake, and synchronous requests.

    Any transport failure kills the process; the next call transparently
    respawns it (no automatic retry of the failed call, since tools/call may
    have side effects).
    """

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self._proc: Optional[subprocess.Popen] = None
        self._buffer = b""
        self._stderr_tail: deque = deque(maxlen=200)

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    pass
            self._kill()

    def last_stderr(self, n: int = 20) -> str:
        return "\n".join(list(self._stderr_tail)[-n:])

    # ── Lifecycle (call with lock held) ──

    def _ensure_started_locked(self, startup_timeout: float) -> None:
        if self._proc is not None and self._proc.poll() is None and self._initialized:
            return
        self._kill()

        cmd = [self.config["command"]] + [str(a) for a in (self.config.get("args") or [])]
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.config.get("env") or {}).items()})
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.config.get("cwd") or None,
                env=env,
            )
        except (OSError, ValueError) as e:
            raise MCPError(
                f"Failed to start MCP server '{self.name}' ({' '.join(cmd)}): {e}. "
                f"Is the command installed?"
            )
        threading.Thread(
            target=self._drain_stderr, args=(self._proc,), daemon=True,
            name=f"mcp-stderr-{self.name}",
        ).start()

        try:
            result = self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "evonic-mcp-client", "version": "1.0.0"},
            }, startup_timeout)
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except MCPError:
            self._kill()
            raise
        self._initialized = True
        logger.info(
            f"MCP server '{self.name}' started (pid {self._proc.pid}, "
            f"protocol {result.get('protocolVersion', 'unknown')})")

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        self._initialized = False
        self._buffer = b""
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
        except Exception:
            pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for line in iter(proc.stderr.readline, b""):
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    logger.debug(f"MCP server '{self.name}' stderr: {text}")
        except Exception:
            pass

    def _stderr_suffix(self) -> str:
        tail = self.last_stderr()
        return f" stderr tail: {tail}" if tail else ""

    # ── JSON-RPC over stdio (call with lock held) ──

    def _send(self, msg: dict) -> None:
        data = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (OSError, ValueError, AttributeError) as e:
            self._kill()
            raise MCPError(
                f"MCP server '{self.name}': failed to write to stdin ({e})."
                f"{self._stderr_suffix()}")

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        self._next_id += 1
        req_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})

        deadline = time.monotonic() + timeout
        while True:
            msg = self._read_message(deadline, method, timeout)
            if "method" in msg and "id" in msg:
                # Server→client request: only ping is supported.
                if msg["method"] == "ping":
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
                else:
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                        "code": -32601,
                        "message": f"Method not supported: {msg['method']}"}})
                continue
            if "method" in msg:
                logger.debug(f"MCP server '{self.name}' notification: {msg['method']}")
                continue
            if msg.get("id") != req_id:
                logger.debug(
                    f"MCP server '{self.name}': ignoring response with "
                    f"unexpected id {msg.get('id')!r}")
                continue
            if "error" in msg:
                err = msg.get("error") or {}
                raise MCPError(
                    f"{method} failed: {err.get('message', 'unknown error')} "
                    f"(code {err.get('code')})", err.get("code"))
            return msg.get("result") or {}

    def _read_message(self, deadline: float, method: str, timeout: float) -> dict:
        """Read the next complete JSON message line, honoring the deadline."""
        while True:
            if b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    logger.warning(
                        f"MCP server '{self.name}': skipping malformed line "
                        f"({line[:200]!r})")
                    continue
                if isinstance(msg, dict):
                    return msg
                logger.warning(f"MCP server '{self.name}': skipping non-object message")
                continue

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill()
                raise MCPError(
                    f"MCP server '{self.name}': {method} timed out after "
                    f"{timeout:g}s.{self._stderr_suffix()}")

            fd = self._proc.stdout.fileno()
            ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                code = self._proc.poll()
                self._kill()
                raise MCPError(
                    f"MCP server '{self.name}' exited unexpectedly "
                    f"(code {code}).{self._stderr_suffix()}")
            self._buffer += chunk


class _SessionExpired(Exception):
    """The HTTP server no longer knows our Mcp-Session-Id (it answered 404)."""


class MCPHttpServer(_MCPServer):
    """One remote MCP server over the Streamable HTTP transport.

    Every JSON-RPC message is a POST to the configured `url`, carrying the
    configured `headers` (e.g. Authorization). The session id the server hands
    out on initialize (Mcp-Session-Id) goes along on every later request; when
    the server forgets it (HTTP 404), the session is re-established once and
    the request re-sent — the server never saw it, so that is safe even for
    tools/call. A reply is either plain JSON or an SSE stream; in a stream,
    server→client requests are answered as over stdio (only ping).
    """

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self._session_id: Optional[str] = None
        self._protocol_version: Optional[str] = None

    def stop(self) -> None:
        with self._lock:
            if self._session_id:
                # Best effort: tell the server the session is over.
                try:
                    urllib.request.urlopen(
                        urllib.request.Request(self.config["url"], method="DELETE",
                                               headers=self._headers()), timeout=3).close()
                except Exception:
                    pass
            self._session_id = None
            self._initialized = False

    # ── Lifecycle (call with lock held) ──

    def _ensure_started_locked(self, startup_timeout: float) -> None:
        if self._initialized:
            return
        self._session_id = None
        self._protocol_version = None
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "evonic-mcp-client", "version": "1.0.0"},
        }, startup_timeout)
        self._protocol_version = result.get("protocolVersion") or PROTOCOL_VERSION
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                   "notifications/initialized", startup_timeout).close()
        self._initialized = True
        logger.info(
            f"MCP server '{self.name}' connected ({self.config['url']}, "
            f"protocol {self._protocol_version})")

    # ── JSON-RPC over HTTP (call with lock held) ──

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            # urllib's default "Python-urllib" UA is rejected by Cloudflare (error 1010),
            # so present as a regular browser. Config `headers` can still override.
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        }
        headers.update(self.config.get("headers") or {})
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        return headers

    def _post(self, msg: dict, method: str, timeout: float):
        """POST one message; returns the open response (caller closes it)."""
        data = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(self.config["url"], data=data, method="POST",
                                     headers=self._headers())
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 404 and self._session_id:
                raise _SessionExpired()
            body = e.read().decode("utf-8", errors="replace").strip()[:500]
            raise MCPError(
                f"MCP server '{self.name}': {method} failed with HTTP {e.code}"
                f"{': ' + body if body else ''}")
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise MCPError(f"MCP server '{self.name}': {method} failed ({reason}).")

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        try:
            return self._request_once(method, params, timeout)
        except _SessionExpired:
            self._initialized = False
            if method == "initialize":
                raise MCPError(f"MCP server '{self.name}': initialize was refused (HTTP 404).")
            logger.info(f"MCP server '{self.name}' session expired — reconnecting")
            self._ensure_started_locked(timeout)
            try:
                return self._request_once(method, params, timeout)
            except _SessionExpired:
                self._initialized = False
                raise MCPError(f"MCP server '{self.name}': session expired again right after reconnecting.")

    def _request_once(self, method: str, params: dict, timeout: float) -> dict:
        self._next_id += 1
        req_id = self._next_id
        deadline = time.monotonic() + timeout
        res = self._post({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
                         method, timeout)
        with res:
            if method == "initialize" and res.headers.get("Mcp-Session-Id"):
                self._session_id = res.headers["Mcp-Session-Id"]
            ctype = (res.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype == "text/event-stream":
                messages = self._sse_messages(res, deadline, method, timeout)
            else:
                try:
                    messages = [json.loads(res.read().decode("utf-8"))]
                except (ValueError, UnicodeDecodeError, OSError) as e:
                    raise MCPError(f"MCP server '{self.name}': {method} returned an unreadable reply ({e}).")
            for msg in messages:
                for m in (msg if isinstance(msg, list) else [msg]):
                    result = self._handle(m, req_id, method, timeout)
                    if result is not None:
                        return result
        raise MCPError(f"MCP server '{self.name}': {method} got no response.")

    def _sse_messages(self, res, deadline: float, method: str, timeout: float):
        """Yield the JSON messages of an SSE stream, honoring the deadline."""
        data: List[str] = []
        while True:
            if time.monotonic() > deadline:
                raise MCPError(f"MCP server '{self.name}': {method} timed out after {timeout:g}s.")
            try:
                raw = res.readline()
            except (OSError, ValueError) as e:
                raise MCPError(f"MCP server '{self.name}': {method} stream failed ({e}).")
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif line == "" and data:
                payload, data = "\n".join(data), []
                try:
                    yield json.loads(payload)
                except ValueError:
                    logger.warning(f"MCP server '{self.name}': skipping malformed event ({payload[:200]!r})")

    def _handle(self, msg, req_id: int, method: str, timeout: float) -> Optional[dict]:
        """The result if `msg` answers our request; answers server requests."""
        if not isinstance(msg, dict):
            return None
        if "method" in msg and "id" in msg:
            if msg["method"] == "ping":
                reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
            else:
                reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32601, "message": f"Method not supported: {msg['method']}"}}
            self._post(reply, method, timeout).close()
            return None
        if "method" in msg:
            logger.debug(f"MCP server '{self.name}' notification: {msg['method']}")
            return None
        if msg.get("id") != req_id:
            return None
        if "error" in msg:
            err = msg.get("error") or {}
            raise MCPError(
                f"{method} failed: {err.get('message', 'unknown error')} "
                f"(code {err.get('code')})", err.get("code"))
        return msg.get("result") or {}


class MCPManager:
    """Registry of MCP server connections keyed by configured server name.

    Server configs come from the mcp_client plugin's MCP_SERVERS variable
    (JSON object). A config edit is picked up on the next call: the cached
    process is stopped and recreated when its config no longer matches.
    """

    def __init__(self):
        self._servers: Dict[str, _MCPServer] = {}
        self._lock = threading.Lock()

    def _load_server_configs(self) -> Dict[str, dict]:
        from backend.plugin_manager import plugin_manager
        raw = plugin_manager.get_plugin_config(PLUGIN_ID).get("MCP_SERVERS", "")
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError as e:
            raise MCPError(f"MCP_SERVERS config is not valid JSON: {e}")
        if not isinstance(parsed, dict):
            raise MCPError(
                "MCP_SERVERS must be a JSON object mapping server name to "
                "{command, args, env, cwd, enabled} or {url, headers, enabled}")

        servers = {}
        for name, cfg in parsed.items():
            if not isinstance(cfg, dict):
                raise MCPError(f"MCP server '{name}': config must be a JSON object")
            url = cfg.get("url")
            if url is not None:
                if cfg.get("command") is not None:
                    raise MCPError(
                        f"MCP server '{name}': give either 'command' (stdio) or 'url' (HTTP), not both")
                if not isinstance(url, str) or not url.strip().startswith(("http://", "https://")):
                    raise MCPError(f"MCP server '{name}': 'url' must be an http(s) URL")
                headers = cfg.get("headers") or {}
                if not isinstance(headers, dict):
                    raise MCPError(f"MCP server '{name}': 'headers' must be an object")
                servers[str(name)] = {
                    "url": url.strip(),
                    "headers": {str(k): str(v) for k, v in headers.items()},
                    "enabled": bool(cfg.get("enabled", True)),
                }
                continue
            command = cfg.get("command")
            if not isinstance(command, str) or not command.strip():
                raise MCPError(f"MCP server '{name}': 'command' (string) or 'url' is required")
            args = cfg.get("args") or []
            env = cfg.get("env") or {}
            if not isinstance(args, list) or not isinstance(env, dict):
                raise MCPError(
                    f"MCP server '{name}': 'args' must be a list and 'env' an object")
            servers[str(name)] = {
                "command": command.strip(),
                "args": args,
                "env": env,
                "cwd": cfg.get("cwd") or None,
                "enabled": bool(cfg.get("enabled", True)),
            }
        return servers

    def get_timeouts(self) -> tuple:
        """(call_timeout, startup_timeout) from plugin config, with fallbacks."""
        try:
            from backend.plugin_manager import plugin_manager
            cfg = plugin_manager.get_plugin_config(PLUGIN_ID)
        except Exception:
            cfg = {}

        def _num(key, default):
            try:
                val = float(cfg.get(key, default))
            except (TypeError, ValueError):
                return default
            return val if val > 0 else default

        return (_num("CALL_TIMEOUT_SECONDS", _DEFAULT_CALL_TIMEOUT),
                _num("STARTUP_TIMEOUT_SECONDS", _DEFAULT_STARTUP_TIMEOUT))

    def server_names(self) -> List[str]:
        return sorted(n for n, c in self._load_server_configs().items() if c["enabled"])

    def list_servers(self) -> List[dict]:
        """Enabled servers as [{name, transport}] — config only, nothing is started."""
        return [{"name": n, "transport": "http" if "url" in c else "stdio"}
                for n, c in sorted(self._load_server_configs().items()) if c["enabled"]]

    def get(self, name: str) -> _MCPServer:
        configs = self._load_server_configs()
        cfg = configs.get(name)
        if cfg is None or not cfg["enabled"]:
            valid = sorted(n for n, c in configs.items() if c["enabled"])
            raise MCPError(
                f"Unknown MCP server '{name}'. Configured servers: "
                f"{', '.join(valid) if valid else '(none)'}")

        with self._lock:
            existing = self._servers.get(name)
            cfg_json = json.dumps(cfg, sort_keys=True)
            if existing is not None and existing.config_json != cfg_json:
                logger.info(f"MCP server '{name}' config changed — restarting")
                existing.stop()
                existing = None
            if existing is None:
                cls = MCPHttpServer if "url" in cfg else MCPServerProcess
                existing = cls(name, cfg)
                self._servers[name] = existing
            return existing

    def shutdown_all(self) -> None:
        with self._lock:
            servers = list(self._servers.values())
            self._servers.clear()
        for srv in servers:
            srv.stop()


mcp_manager = MCPManager()
atexit.register(mcp_manager.shutdown_all)
