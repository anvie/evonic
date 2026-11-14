"""The sandbox exec backend speaks the contract devproxy publishes (PIT-400).

One fake machine: a stdlib HTTP server that serves `POST /exec` exactly as
`sandbox/cmd/devproxy/main.go` `serveExec` documents — bearer token,
`{command, cwd, timeout}`, `application/x-ndjson` frames, exit code last.
No Fly, no network.
"""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from flask import Flask

from backend.tools.lib.backends.sandbox_exec_backend import (
    SandboxExecBackend, clear_session, register_session, session_for,
)
from backend.tools.lib.exec_backend import registry
from plugins.agentapi.routes import _apply_sandbox_exec

TOKEN = 'session-token'


class _Handler(BaseHTTPRequestHandler):
    workspace = None
    token = TOKEN
    seen = []

    def log_message(self, *args):  # keep pytest output clean
        pass

    def do_POST(self):
        if self.path != '/exec':
            self.send_error(404)
            return
        if self.headers.get('Authorization') != f'Bearer {self.token}':
            self.send_error(401, 'unauthorized')
            return
        req = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        command = req.get('command') or ''
        if not command:
            self.send_error(400, 'command is required')
            return
        _Handler.seen.append(req)
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson')
        self.end_headers()

        def emit(frame):
            self.wfile.write((json.dumps(frame) + '\n').encode())
            self.wfile.flush()

        cwd = req.get('cwd') or self.workspace
        proc = subprocess.run(['sh', '-c', command], cwd=cwd,
                              capture_output=True, text=True)
        if proc.stdout:
            emit({'stream': 'stdout', 'data': proc.stdout})
        if proc.stderr:
            emit({'stream': 'stderr', 'data': proc.stderr})
        emit({'exitCode': proc.returncode, 'durationMs': 1})


@pytest.fixture()
def machine(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    _Handler.workspace = str(workspace)
    _Handler.seen = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_address[1]}/exec'
    try:
        yield SandboxExecBackend(
            config={'url': url, 'token': TOKEN, 'workspace_path': str(workspace)})
    finally:
        server.shutdown()
        server.server_close()


def test_run_bash_streams_and_reports_the_exit_code(machine):
    seen = []
    r = machine.run_bash('echo halo; echo oops >&2', 10, {}, on_output=seen.append)
    assert r['exit_code'] == 0
    assert r['stdout'].strip() == 'halo'
    assert 'oops' in r['stderr']
    assert ''.join(seen).strip() == 'halo'

    failed = machine.run_bash('exit 3', 10, {})
    assert failed['exit_code'] == 3, failed


def test_a_file_written_through_the_backend_is_the_one_the_machine_reads(machine):
    assert machine.write_file('src/app.js', 'console.log(1)\n') == {'ok': True}
    assert machine.read_file('src/app.js') == {'content': 'console.log(1)\n'}
    assert machine.file_exists('src/app.js') is True
    assert machine.file_stat('src/app.js') == {'exists': True, 'size': 15, 'is_binary': False}
    assert machine.make_dirs('dist/deep') == {'ok': True}
    assert machine.delete_file('src/app.js') == {'ok': True}
    assert machine.file_exists('src/app.js') is False


def test_unconfigured_exec_answers_instead_of_raising():
    r = SandboxExecBackend(config={}).run_bash('id', 5, {})
    assert r['exit_code'] == -1
    assert 'not configured' in r['stderr']


def test_the_registry_picks_the_machine_over_the_agents_workplace(machine):
    """PIT-401: the request's machine wins over the SSH workplace it replaces."""
    url = machine.status()['url']
    workspace = str(machine.status()['workspace'])
    ctx = {'id': 'agent-1', 'workplace_id': 'some-ssh-workplace', 'sandbox_enabled': 1}
    try:
        register_session('agent-1', url, TOKEN, workspace)
        backend = registry.get_backend('session-1', ctx)
        assert isinstance(backend, SandboxExecBackend), backend
        assert backend.run_bash('echo in-machine', 10, {})['stdout'].strip() == 'in-machine'
        assert _Handler.seen[-1]['cwd'] == workspace

        # The dashboard's flag off: no headers, no machine, the workplace again.
        clear_session('agent-1')
        assert session_for('agent-1') == {}
    finally:
        clear_session('agent-1')


def test_the_chat_request_headers_are_what_registers_the_machine(machine):
    """The header names are the cross-repo contract with the dashboard (agent.rs)."""
    app = Flask(__name__)
    url = machine.status()['url']
    with app.test_request_context(headers={'X-Pitura-Exec-Url': url,
                                          'X-Pitura-Exec-Token': TOKEN}):
        _apply_sandbox_exec('agent-2')
    assert session_for('agent-2')['url'] == url
    assert session_for('agent-2')['workspace_path'] == '/workspace'

    # Same request shape without the headers (flag off) forgets the machine.
    with app.test_request_context():
        _apply_sandbox_exec('agent-2')
    assert session_for('agent-2') == {}
