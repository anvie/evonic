"""
CloudWorkplaceBackend — executes commands on a remote computer running the Evonet connector
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

import json
import shlex
import threading
import uuid
import logging

from backend.tools.lib.exec_backend import ExecutionBackend, file_stat_code, parse_file_stat_output

_logger = logging.getLogger(__name__)


class CloudWorkplaceBackend(ExecutionBackend):
    """Executes commands via JSON-RPC over the Evonet WebSocket connection."""

    def __init__(self, workplace_id: str, workspace: str = None):
        self._workplace_id = workplace_id
        self._workspace = workspace
        self._ws = None                          # set by on_ws_connected()
        self._ws_lock = threading.Lock()
        self._pending: dict[str, threading.Event] = {}
        self._results: dict[str, dict] = {}
        self._rpc_lock = threading.Lock()

    # -------------------------------------------------------------------------
    # Called by ConnectorRelay when Evonet connects / disconnects
    # -------------------------------------------------------------------------

    def on_ws_connected(self, ws) -> None:
        with self._ws_lock:
            self._ws = ws

    def on_ws_disconnected(self) -> None:
        with self._ws_lock:
            self._ws = None
        # Unblock all pending calls with an error
        with self._rpc_lock:
            for req_id, event in list(self._pending.items()):
                self._results[req_id] = {
                    'stdout': '', 'stderr': 'Evonet disconnected.', 'exit_code': -1
                }
                event.set()

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
            event = self._pending.pop(req_id, None)
        if event:
            event.set()

    # -------------------------------------------------------------------------
    # Internal RPC call helper
    # -------------------------------------------------------------------------

    def _call(self, method: str, params: dict, timeout: int = 65) -> dict:
        with self._ws_lock:
            ws = self._ws
        if ws is None:
            return {
                'stdout': '', 'stderr': 'Evonet is not connected to this Workplace.',
                'exit_code': -1, 'error': 'evonet_offline'
            }
        req_id = uuid.uuid4().hex
        msg = json.dumps({'id': req_id, 'method': method, 'params': params})
        event = threading.Event()
        with self._rpc_lock:
            self._pending[req_id] = event
        try:
            ws.send(msg)
        except Exception as e:
            with self._rpc_lock:
                self._pending.pop(req_id, None)
            return {'stdout': '', 'stderr': f'Send error: {e}', 'exit_code': -1}

        if not event.wait(timeout=timeout):
            with self._rpc_lock:
                self._pending.pop(req_id, None)
                self._results.pop(req_id, None)
            return {'stdout': '', 'stderr': f'Evonet did not respond within {timeout}s', 'exit_code': -1}

        with self._rpc_lock:
            return self._results.pop(req_id, {'exit_code': -1, 'stderr': 'No result'})

    # -------------------------------------------------------------------------
    # ExecutionBackend interface
    # -------------------------------------------------------------------------

    def run_bash(self, script: str, timeout: int, env: dict) -> dict:
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
        # Evonet auto-creates parent directories
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

    def make_dirs(self, path: str) -> dict:
        r = self.run_bash(f'mkdir -p {shlex.quote(path)}', 10, {})
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or r.get('error', 'mkdir failed')}
        return {'ok': True}

    def destroy(self) -> dict:
        with self._ws_lock:
            self._ws = None
        return {'result': 'ok', 'detail': 'CloudWorkplaceBackend released (Evonet connection not closed).'}

    def status(self) -> dict:
        with self._ws_lock:
            connected = self._ws is not None
        return {
            'backend': 'cloud_workplace',
            'workplace_id': self._workplace_id,
            'workspace': self._workspace,
            'evonet_connected': connected,
        }
