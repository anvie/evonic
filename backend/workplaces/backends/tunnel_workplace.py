"""
TunnelWorkplaceBackend — executes commands on a remote computer running the Evonet connector
program via a WebSocket relay.

The WebSocket connection is established by Evonet (outbound), managed by ConnectorRelay.
WorkplaceManager calls on_ws_connected() / on_ws_disconnected() as the Evonet program
connects and disconnects.

JSON-RPC protocol (over WebSocket text frames):
  Request  (Evonic → Evonet): {"id": "<uuid>", "method": "<method>", "params": {...}}
  Response (Evonet → Evonic): {"id": "<uuid>", "ok": true,  "result": {...}}
                            | {"id": "<uuid>", "ok": false, "error": "<msg>"}
  Ping/pong:                  {"type": "ping"} / {"type": "pong"}

Supported methods:
  exec_bash    params: {script, timeout, env, cwd}
  exec_python  params: {code,   timeout, env, cwd}
  read_file    params: {path}
  write_file   params: {path, content, mode}
"""

import base64
import binascii
import json
import os
import shlex
import threading
import uuid
import logging

from backend.tools.lib.exec_backend import ExecutionBackend, file_stat_code, parse_file_stat_output

_logger = logging.getLogger(__name__)

# How long a single tool call keeps waiting through a WebSocket disconnect before
# giving up. Within this window, Evonet reconnects (its backoff caps at 30s) and
# the request is re-sent; Evonet deduplicates by request id so the command runs
# exactly once. Only applies to clients that advertise idempotent-replay support.
DISCONNECT_GRACE = 90


class TunnelWorkplaceBackend(ExecutionBackend):
    """Executes commands via JSON-RPC over the Evonet WebSocket connection."""

    def __init__(self, workplace_id: str, workspace: str = None):
        self._workplace_id = workplace_id
        self._workspace = workspace
        self._ws = None                          # set by on_ws_connected()
        self._ws_lock = threading.Lock()
        self._replay_ok = False                  # set by on_ws_connected()
        # req_id → {'event': Event, 'msg': str}. The msg is retained so the
        # request can be re-sent verbatim after a reconnect.
        self._pending: dict[str, dict] = {}
        self._results: dict[str, dict] = {}
        self._rpc_lock = threading.Lock()

    def set_workspace(self, path: str) -> None:
        """Update the tunnel workspace path (for /cd command)."""
        self._workspace = path

    def resolve_path(self, path: str) -> str:
        """Remap server-resolved paths to the remote Evonet workspace.

        resolve_workspace_path may have resolved a relative or /workspace-prefixed
        path against the server-side SANDBOX_WORKSPACE.  Detect that and remap to
        the remote workspace so the Evonet executor receives a correct path.
        """
        if not self._workspace or not path:
            return path

        import os
        try:
            from config import SANDBOX_WORKSPACE as server_root
        except ImportError:
            server_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), '..', '..', '..')
            )
        server_root = os.path.abspath(server_root)

        if path.startswith(server_root + os.sep):
            rel = path[len(server_root) + 1:]
            return self._workspace.rstrip('/') + '/' + rel
        if path == server_root:
            return self._workspace

        return path

    # -------------------------------------------------------------------------
    # Called by ConnectorRelay when Evonet connects / disconnects
    # -------------------------------------------------------------------------

    def on_ws_connected(self, ws, replay_ok: bool = False) -> None:
        with self._ws_lock:
            self._ws = ws
            self._replay_ok = replay_ok
        if not replay_ok:
            return
        # Re-send any in-flight requests on the fresh socket. Evonet dedupes by
        # request id, so a re-send returns the cached result (or attaches to the
        # still-running command) rather than executing it again.
        with self._rpc_lock:
            pending = [(rid, rec['msg']) for rid, rec in self._pending.items()]
        for req_id, msg in pending:
            try:
                ws.send(msg)
            except Exception as e:
                _logger.warning("Resend of pending request %s failed: %s", req_id, e)

    def on_ws_disconnected(self) -> None:
        with self._ws_lock:
            self._ws = None
            replay_ok = self._replay_ok
        # With a replay-capable Evonet, keep pending calls waiting: they are
        # re-sent on reconnect and answered, or time out against the grace
        # deadline in _call(). Only legacy clients fail fast here.
        if replay_ok:
            return
        with self._rpc_lock:
            for req_id, rec in list(self._pending.items()):
                self._results[req_id] = {
                    'stdout': '', 'stderr': 'Evonet disconnected.', 'exit_code': -1
                }
                rec['event'].set()

    def on_message(self, data: dict) -> None:
        """Called by ConnectorRelay when a JSON response arrives from Evonet."""
        req_id = data.get('id')
        if not req_id:
            return
        result = data.get('result') if data.get('ok') else {
            'stdout': '', 'stderr': data.get('error', 'Unknown error'), 'exit_code': -1
        }
        with self._rpc_lock:
            self._results[req_id] = result or {}
            rec = self._pending.pop(req_id, None)
        if rec:
            rec['event'].set()

    # -------------------------------------------------------------------------
    # Internal RPC call helper
    # -------------------------------------------------------------------------

    def _call(self, method: str, params: dict, timeout: int = 65) -> dict:
        with self._ws_lock:
            ws = self._ws
            replay_ok = self._replay_ok

        # Without replay support, preserve the original fast-fail when offline.
        if ws is None and not replay_ok:
            return {
                'stdout': '', 'stderr': 'Evonet is not connected to this Workplace.',
                'exit_code': -1, 'error': 'evonet_offline'
            }

        req_id = uuid.uuid4().hex
        msg = json.dumps({'id': req_id, 'method': method, 'params': params})
        event = threading.Event()
        with self._rpc_lock:
            self._pending[req_id] = {'event': event, 'msg': msg}

        # Send now if connected. If the send fails (or we're offline) and the
        # client supports replay, leave the request pending — on_ws_connected
        # re-sends it once Evonet reconnects.
        if ws is not None:
            try:
                ws.send(msg)
            except Exception as e:
                if not replay_ok:
                    with self._rpc_lock:
                        self._pending.pop(req_id, None)
                    return {'stdout': '', 'stderr': f'Send error: {e}', 'exit_code': -1}

        # Wait for the response. Replay-capable clients get an extra grace window
        # so a transient disconnect (Evonet reconnects and the request is re-sent
        # by on_ws_connected) doesn't surface as an error to the agent.
        grace = DISCONNECT_GRACE if replay_ok else 0
        if not event.wait(timeout=timeout + grace):
            with self._rpc_lock:
                self._pending.pop(req_id, None)
                self._results.pop(req_id, None)
            return {'stdout': '', 'stderr': f'Evonet did not respond within {timeout}s', 'exit_code': -1}

        with self._rpc_lock:
            return self._results.pop(req_id, {'exit_code': -1, 'stderr': 'No result'})

    # -------------------------------------------------------------------------
    # ExecutionBackend interface
    # -------------------------------------------------------------------------

    def run_bash(self, script: str, timeout: int, env: dict, on_output=None) -> dict:
        # on_output (live streaming) is not supported over the Evonet relay;
        # output is returned batched. Accepted for API compatibility.
        params = {'script': script, 'timeout': timeout, 'env': env or {}}
        if self._workspace:
            params['cwd'] = self._workspace
        return self._call('exec_bash', params, timeout=timeout + 10)

    def run_python(self, code: str, timeout: int, env: dict) -> dict:
        params = {'code': code, 'timeout': timeout, 'env': env or {}}
        if self._workspace:
            params['cwd'] = self._workspace
        return self._call('exec_python', params, timeout=timeout + 10)

    # ------------------------------------------------------------------
    # File I/O — native RPC + shell fallbacks
    # ------------------------------------------------------------------

    def file_exists(self, path: str) -> bool:
        r = self.run_bash(f'test -e {shlex.quote(path)} && echo yes || echo no', 5, {})
        return r.get('stdout', '').strip() == 'yes'

    def file_stat(self, path: str) -> dict:
        r = self.run_python(file_stat_code(path), 10, {})
        return parse_file_stat_output(r.get('stdout', ''))

    def read_file(self, path: str) -> dict:
        r = self._call('read_file', {'path': path}, timeout=30)
        if 'error' in r or ('exit_code' in r and r['exit_code'] < 0):
            return {'error': r.get('stderr', '') or r.get('error', 'read failed')}
        return {'content': r.get('content', '')}

    def write_file(self, path: str, content: str, create_dirs: bool = True) -> dict:
        if create_dirs:
            self.make_dirs(os.path.dirname(path) or '.')
        r = self._call('write_file', {'path': path, 'content': content}, timeout=30)
        if 'error' in r or ('exit_code' in r and r['exit_code'] < 0):
            return {'error': r.get('stderr', '') or r.get('error', 'write failed')}
        return {'ok': True}

    def read_file_b64(self, path: str, offset: int = 0, size: int = 0) -> dict:
        """Read a chunk of a file as base64 from the remote via RPC."""
        r = self._call('read_file_b64', {'path': path, 'offset': offset, 'size': size}, timeout=60)
        if 'error' in r or ('exit_code' in r and r['exit_code'] < 0):
            return {'error': r.get('stderr', '') or r.get('error', 'read_file_b64 failed')}
        return r

    def write_file_b64(self, path: str, data_b64: str, offset: int = 0, is_last: bool = True) -> dict:
        """Write a base64-encoded chunk to a file on the remote via RPC."""
        mode = "create" if offset == 0 else "append"
        r = self._call('write_file_b64', {
            'path': path, 'data': data_b64, 'offset': offset,
            'is_last': is_last, 'mode': mode,
        }, timeout=60)
        if 'error' in r or ('exit_code' in r and r['exit_code'] < 0):
            return {'error': r.get('stderr', '') or r.get('error', 'write_file_b64 failed')}
        return {'ok': True}

    def cat_file_bytes(self, path: str) -> dict:
        """Read a file as raw bytes from the remote Evonet via RPC.

        Uses the existing read_file_b64 RPC call and base64-decodes the result.
        """
        import base64
        r = self.read_file_b64(path)
        if 'error' in r:
            return r
        data = r.get('data', '')
        if not data:
            return {'bytes': b''}
        try:
            # RPC transports may wrap Base64 output or omit optional terminal
            # padding. Remove transport whitespace and restore only that padding
            # before using strict decoding, so malformed data remains rejected.
            normalized = ''.join(data.split())
            normalized += '=' * (-len(normalized) % 4)
            return {'bytes': base64.b64decode(normalized, validate=True)}
        except (TypeError, ValueError, binascii.Error) as e:
            return {'error': f'base64 decode failed: {e}'}

    def delete_file(self, path: str) -> dict:
        """Delete a file on the remote Evonet via shell."""
        r = self.run_bash(f'rm -f {shlex.quote(path)}', 10, {})
        # rm -f succeeds even if the file doesn't exist
        return {'ok': True}

    def write_file_bytes(self, path: str, data: bytes, create_dirs: bool = True) -> dict:
        """Write raw bytes to a file on the remote Evonet via RPC.

        Uses the existing write_file_b64 RPC for binary-safe transfer.
        """
        import base64
        if create_dirs:
            self.make_dirs(os.path.dirname(path) or '.')
        encoded = base64.b64encode(data).decode('ascii')
        return self.write_file_b64(path, encoded)

    def make_dirs(self, path: str) -> dict:
        r = self.run_bash(f'mkdir -p {shlex.quote(path)}', 10, {})
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or r.get('error', 'mkdir failed')}
        return {'ok': True}

    def destroy(self) -> dict:
        with self._ws_lock:
            self._ws = None
        return {'result': 'ok', 'detail': 'TunnelWorkplaceBackend released (Evonet connection not closed).'}

    def status(self) -> dict:
        with self._ws_lock:
            connected = self._ws is not None
        return {
            'backend': 'tunnel_workplace',
            'workplace_id': self._workplace_id,
            'workspace': self._workspace,
            'evonet_connected': connected,
        }
