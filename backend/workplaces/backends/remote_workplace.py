"""
RemoteWorkplaceBackend — wraps SSHBackend for a Workplace execution environment.

Unlike a manual per-session SSH connection, this backend is managed by WorkplaceManager:
it auto-connects when the workplace is first accessed and is shared across all sessions
using the same workplace.

Config keys expected in workplace.config:
  host            — SSH hostname or IP (required)
  port            — SSH port (default: 22)
  username        — SSH username (required)
  auth_type       — "password" | "key" (default: "key")
  password        — used when auth_type="password"
  key_path        — path to private key file, used when auth_type="key"
  passphrase      — optional passphrase for encrypted key
  workspace_path  — remote working directory (optional, used as cwd)
"""

import os
import base64
import shlex

from backend.tools.lib.exec_backend import ExecutionBackend, file_stat_code, parse_file_stat_output


class RemoteWorkplaceBackend(ExecutionBackend):
    """Executes commands on a remote server via SSH, auto-connecting from workplace config."""

    def __init__(self, config: dict, workplace_id: str = ''):
        self._config = config
        self._workspace = config.get('workspace_path')
        self._workplace_id = workplace_id
        self._ssh = None
        self._connect()

    def _connect(self):
        from backend.tools.lib.backends.ssh_backend import SSHBackend
        cfg = self._config
        ssh_session_id = f'workplace:{self._workplace_id}' if self._workplace_id else ''
        self._ssh = SSHBackend(
            host=cfg['host'],
            username=cfg['username'],
            port=int(cfg.get('port', 22)),
            password=cfg.get('password') if cfg.get('auth_type') == 'password' else None,
            key_path=cfg.get('key_path') if cfg.get('auth_type') != 'password' else None,
            passphrase=cfg.get('passphrase'),
            session_id=ssh_session_id,
        )

    def _cwd_prefix(self) -> str:
        if self._workspace:
            escaped = self._workspace.replace("'", "'\\\\''")
            return f"cd '{escaped}' && "
        return ''

    def run_bash(self, script: str, timeout: int, env: dict, on_output=None) -> dict:
        prefixed = self._cwd_prefix() + script if self._workspace else script
        return self._ssh.run_bash(prefixed, timeout, env, on_output=on_output)

    def run_python(self, code: str, timeout: int, env: dict) -> dict:
        # Ensure evonic helpers are available on the remote (lazy upload on first call)
        self._ssh._ensure_evonic_on_remote()
        remote_evonic = os.path.expanduser('~/.evonic/evonic')
        if self._workspace:
            escaped = self._workspace.replace("'", "'\\''")
            # SSHBackend's run_python pipes code to python3, but we need bash wrapping;
            # call run_bash with explicit python3 inline execution
            merged = dict(env or {})
            merged['PYTHONPATH'] = f"{remote_evonic}:{merged.get('PYTHONPATH', '')}".rstrip(':')
            env_exports = ' '.join(f'{k}={v!r}' for k, v in merged.items())
            bash_script = f"""{'export ' + env_exports + ' && ' if env_exports else ''}cd '{escaped}' && python3 - <<'__PYEOF__'
{code}
__PYEOF__"""
            return self._ssh.run_bash(bash_script, timeout, {})
        return self._ssh.run_python(code, timeout, env)

    def set_workspace(self, path: str) -> None:
        """Update the remote workspace path (for /cd command)."""
        self._workspace = path

    def destroy(self) -> dict:
        if self._ssh:
            return self._ssh.destroy()
        return {'result': 'ok'}

    def status(self) -> dict:
        if self._ssh:
            s = self._ssh.status()
            s['backend'] = 'remote_workplace'
            s['workspace'] = self._workspace
            return s
        return {'backend': 'remote_workplace', 'status': 'disconnected'}

    def _resolve_path(self, path: str) -> str:
        """Make relative paths absolute against workspace_path."""
        if not path.startswith('/') and self._workspace:
            return self._workspace.rstrip('/') + '/' + path
        return path

    def file_exists(self, path: str) -> bool:
        path = self._resolve_path(path)
        r = self._ssh.run_bash(f'test -e {shlex.quote(path)} && echo yes || echo no', 5, {})
        return r.get('stdout', '').strip() == 'yes'

    def file_stat(self, path: str) -> dict:
        path = self._resolve_path(path)
        r = self._ssh.run_python(file_stat_code(path), 10, {})
        return parse_file_stat_output(r.get('stdout', ''))

    def read_file(self, path: str) -> dict:
        path = self._resolve_path(path)
        r = self._ssh.run_bash(f'cat {shlex.quote(path)}', 30, {})
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or r.get('error', 'read failed')}
        return {'content': r.get('stdout', '')}

    def write_file(self, path: str, content: str, create_dirs: bool = True) -> dict:
        path = self._resolve_path(path)
        encoded = base64.b64encode(content.encode('utf-8')).decode('ascii')
        script = ''
        if create_dirs:
            dir_path = path.rsplit('/', 1)[0] if '/' in path else ''
            if dir_path:
                script += f'mkdir -p {shlex.quote(dir_path)}\n'
        script += f'echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}\n'
        r = self._ssh.run_bash(script, 30, {})
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or r.get('error', 'write failed')}
        return {'ok': True}

    def cat_file_bytes(self, path: str) -> dict:
        """Read a file as raw bytes from the remote host via SSH (delegates to SSHBackend)."""
        path = self._resolve_path(path)
        return self._ssh.cat_file_bytes(path)

    def delete_file(self, path: str) -> dict:
        """Delete a file on the remote host via SSH."""
        path = self._resolve_path(path)
        r = self._ssh.run_bash(f'rm {shlex.quote(path)}', 10, {})
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or r.get('error', 'rm failed')}
        return {'ok': True}

    def write_file_bytes(self, path: str, data: bytes, create_dirs: bool = True) -> dict:
        """Write raw bytes to a file on the remote host via SSH."""
        path = self._resolve_path(path)
        import base64
        encoded = base64.b64encode(data).decode('ascii')
        script = ''
        if create_dirs:
            dir_path = path.rsplit('/', 1)[0] if '/' in path else ''
            if dir_path:
                script += f'mkdir -p {shlex.quote(dir_path)}\n'
        script += f'echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}\n'
        r = self._ssh.run_bash(script, 30, {})
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or r.get('error', 'write_file_bytes failed')}
        return {'ok': True}

    def make_dirs(self, path: str) -> dict:
        path = self._resolve_path(path)
        r = self._ssh.run_bash(f'mkdir -p {shlex.quote(path)}', 10, {})
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or r.get('error', 'mkdir failed')}
        return {'ok': True}
