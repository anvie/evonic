"""
SSHBackend — runs bash and Python on a remote server via SSH (paramiko).

Authentication priority:
  1. password       — if password arg is provided
  2. key_path       — explicit key file (+ optional passphrase)
  3. auto-discover  — paramiko tries ~/.ssh/id_* and any loaded ssh-agent keys
                      (look_for_keys=True, allow_agent=True) — same as `ssh user@host`
"""

import base64
import logging
import os
import shlex
import threading
import time

from backend.tools.lib.exec_backend import ExecutionBackend, truncate, file_stat_code, parse_file_stat_output
from backend.tools.lib.process_tracker import process_tracker

logger = logging.getLogger(__name__)

_MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB
_MAX_RETRIES = 5

# Path to local runpy_helpers directory (evonic package) for uploading to remote.
_HELPERS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'runpy_helpers'))
_REMOTE_EVONIC_DIR = '~/.evonic/evonic'


class SSHBackend(ExecutionBackend):
    """Executes bash/python on a remote server via SSH."""

    def __init__(self, host: str, username: str, port: int = 22,
                 password: str = None, key_path: str = None, passphrase: str = None,
                 session_id: str = ''):
        try:
            import paramiko
        except ImportError:
            raise RuntimeError("paramiko is required for SSHBackend. Run: pip install paramiko")

        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._key_path = key_path
        self._passphrase = passphrase
        self._connected_at = None
        self._last_used = None
        self._session_id = session_id
        self._kill_flag = threading.Event()
        self._remote_pid = None
        self._active_channel = None
        self._evonic_installed = False

        self._client = paramiko.SSHClient()
        self._load_host_keys()
        self._client.set_missing_host_key_policy(paramiko.WarningPolicy())
        self._connect()

    def _load_host_keys(self):
        """Load known_hosts from standard locations for host key verification.

        Tries the user's ~/.ssh/known_hosts first, then falls back to the
        system-wide known_hosts. If neither exists, the client will still
        connect (via WarningPolicy) but will log a warning for unknown hosts.
        """
        import paramiko

        candidates = [
            os.path.expanduser('~/.ssh/known_hosts'),
        ]
        # System-wide known_hosts (platform-dependent)
        if os.name == 'posix':
            candidates.extend([
                '/etc/ssh/ssh_known_hosts',
                '/etc/ssh/known_hosts',
            ])

        for path in candidates:
            if os.path.isfile(path):
                try:
                    self._client.load_host_keys(path)
                    logger.debug("[ssh] Loaded host keys from %s", path)
                    return
                except Exception:
                    logger.debug("[ssh] Failed to load host keys from %s", path)

        logger.warning(
            "[ssh] No known_hosts file found; unknown hosts will be accepted "
            "with a warning (MITM risk). Add host keys via: ssh-keyscan -H %s >> ~/.ssh/known_hosts",
            self._host,
        )

    def _connect(self):
        import paramiko

        kwargs = dict(
            hostname=self._host,
            port=self._port,
            username=self._username,
            timeout=300,
        )

        if self._password:
            # Password auth — explicit, no key discovery
            kwargs['password'] = self._password
            kwargs['look_for_keys'] = False
            kwargs['allow_agent'] = False
        elif self._key_path:
            # Explicit key file
            expanded = os.path.expanduser(self._key_path)
            kwargs['key_filename'] = expanded
            kwargs['passphrase'] = self._passphrase
            kwargs['look_for_keys'] = False
            kwargs['allow_agent'] = True
        else:
            # Auto-discover: try ssh-agent + ~/.ssh/id_* keys (same as `ssh` CLI)
            kwargs['look_for_keys'] = True
            kwargs['allow_agent'] = True

        self._client.connect(**kwargs)
        self._client.get_transport().set_keepalive(15)
        self._connected_at = time.time()
        self._last_used = time.time()
        logger.info(
            "[ssh_connect] Connected host=%s port=%s user=%s keepalive=15s",
            self._host, self._port, self._username,
        )

    def _exec_once(self, command: str, stdin_data: str, timeout: int,
                   _track_pid: bool = False) -> dict:
        """Single execution attempt. Returns _connection_lost=True when transport dies mid-run.

        Tracks the remote PID so kill() can send SIGTERM.
        """
        # Health check before exec
        transport = self._client.get_transport()
        if transport is None or not transport.is_active():
            return {'error': 'Transport not active before exec', 'exit_code': -1, '_connection_lost': True}

        # Clear kill flag for this execution
        self._kill_flag.clear()
        self._remote_pid = None
        self._active_channel = None

        # When tracking PID, prepend an echo of $$ to capture the remote process PID
        effective_cmd = command
        if _track_pid:
            effective_cmd = 'echo "EVONIC_PID:$$"; ' + command

        t0 = time.time()
        try:
            stdin, stdout, stderr = self._client.exec_command(effective_cmd, timeout=timeout)
            self._active_channel = stdout.channel
            if stdin_data:
                stdin.write(stdin_data)
                stdin.channel.shutdown_write()

            channel = stdout.channel
            deadline = t0 + timeout
            poll_count = 0
            last_transport_log = t0

            while not channel.exit_status_ready():
                now = time.time()

                # Check for external kill request
                if self._kill_flag.is_set():
                    elapsed_so_far = round(now - t0, 1)
                    logger.info(
                        "[ssh_exec] KILL requested host=%s elapsed=%.1fs",
                        self._host, elapsed_so_far,
                    )
                    channel.close()
                    return {
                        'error': 'Execution stopped by user',
                        'exit_code': -9,
                        'execution_time': elapsed_so_far,
                    }

                # Detect silent connection drop — keepalive marks transport inactive within ~15s
                tr = self._client.get_transport()
                if tr is None or not tr.is_active():
                    elapsed_so_far = round(now - t0, 1)
                    logger.warning(
                        "[ssh_exec] Transport died mid-execution host=%s elapsed=%.1fs poll_count=%d",
                        self._host, elapsed_so_far, poll_count,
                    )
                    channel.close()
                    return {'error': 'SSH connection lost during execution', 'exit_code': -1, '_connection_lost': True}

                # Periodic heartbeat log every 10s
                if now - last_transport_log >= 10:
                    logger.warning(
                        "[ssh_exec] Still waiting host=%s elapsed=%.1fs channel_closed=%s "
                        "poll_count=%d deadline_in=%.1fs",
                        self._host, round(now - t0, 1), channel.closed, poll_count, deadline - now,
                    )
                    last_transport_log = now

                if now > deadline:
                    logger.error(
                        "[ssh_exec] TIMEOUT host=%s after %ss", self._host, timeout,
                    )
                    channel.close()
                    return {'error': f'Execution timed out after {timeout}s', 'exit_code': -1}

                poll_count += 1
                time.sleep(0.05)

            exit_code = channel.recv_exit_status()
            raw_out = stdout.read().decode('utf-8', errors='replace')
            out = truncate(raw_out, _MAX_OUTPUT_BYTES)
            err = truncate(stderr.read().decode('utf-8', errors='replace'), _MAX_OUTPUT_BYTES)

            # Parse remote PID from first line if tracking
            if _track_pid:
                lines = raw_out.split('\n', 1)
                if lines[0].startswith('EVONIC_PID:'):
                    try:
                        self._remote_pid = int(lines[0].split(':', 1)[1].strip())
                        logger.info(
                            "[ssh_exec] Captured remote PID=%s host=%s",
                            self._remote_pid, self._host,
                        )
                    except (ValueError, IndexError):
                        self._remote_pid = None
                    # Strip the PID line from output
                    if len(lines) > 1:
                        out = truncate(lines[1], _MAX_OUTPUT_BYTES)
                    else:
                        out = ''

        except Exception as e:
            elapsed = round(time.time() - t0, 3)
            logger.error(
                "[ssh_exec] EXCEPTION host=%s after %.3fs err=%r type=%s",
                self._host, elapsed, str(e), type(e).__name__,
            )
            self._active_channel = None
            return {'error': str(e), 'exit_code': -1}

        elapsed = round(time.time() - t0, 3)
        self._last_used = time.time()
        self._active_channel = None
        logger.debug("[ssh_exec] DONE host=%s exit_code=%s elapsed=%ss", self._host, exit_code, elapsed)
        return {
            'stdout': out,
            'stderr': err,
            'exit_code': exit_code,
            'execution_time': elapsed,
        }

    def _force_stop(self):
        """Kill the currently running remote process via SIGTERM."""
        # Set the kill flag so _exec_once's polling loop exits
        self._kill_flag.set()

        # Try to close the active channel (terminates remote process)
        channel = self._active_channel
        if channel is not None and not channel.closed:
            try:
                channel.close()
            except Exception:
                pass

        # Also send SIGTERM to the remote PID if known
        if self._remote_pid:
            try:
                kill_stdin, kill_stdout, kill_stderr = self._client.exec_command(
                    f'kill -TERM {self._remote_pid} 2>/dev/null; '
                    f'sleep 0.1; '
                    f'kill -KILL {self._remote_pid} 2>/dev/null',
                    timeout=5,
                )
                kill_stdout.channel.close()
            except Exception:
                pass

    def _exec(self, command: str, stdin_data: str, timeout: int,
              _track_pid: bool = False) -> dict:
        """Run a command over SSH with transparent reconnect + exponential backoff on connection loss.

        The caller (and the agent's LLM loop) never sees a mid-run disconnect — this method
        blocks through reconnects and re-runs the command on the fresh connection.
        Up to _MAX_RETRIES (5) reconnect attempts; backoff: 1, 2, 4, 8, 16 seconds.

        Args:
            _track_pid: If True, prepend ``echo "EVONIC_PID:$$";`` to command
                and parse the remote PID from the first stdout line.
        """
        for attempt in range(_MAX_RETRIES + 1):
            result = self._exec_once(command, stdin_data, timeout, _track_pid=_track_pid)

            if not result.pop('_connection_lost', False):
                # Success or non-connection error (timeout, bad exit code, etc.) — return as-is
                return result

            # Connection lost — decide whether to retry
            if attempt >= _MAX_RETRIES:
                logger.error(
                    "[ssh_exec] Connection lost, max retries (%d) exhausted host=%s",
                    _MAX_RETRIES, self._host,
                )
                return {'error': f'SSH connection lost after {_MAX_RETRIES} reconnect attempts', 'exit_code': -1}

            wait = 2 ** attempt  # 1, 2, 4, 8, 16s
            logger.warning(
                "[ssh_exec] Connection lost — reconnecting in %ds (attempt %d/%d) host=%s",
                wait, attempt + 1, _MAX_RETRIES, self._host,
            )
            time.sleep(wait)

            try:
                self._connect()
                logger.info(
                    "[ssh_exec] Reconnected, retrying command (attempt %d/%d) host=%s",
                    attempt + 1, _MAX_RETRIES, self._host,
                )
            except Exception as e:
                logger.error(
                    "[ssh_exec] Reconnect attempt %d/%d failed host=%s err=%s",
                    attempt + 1, _MAX_RETRIES, self._host, e,
                )
                # Loop continues — next iteration will hit the transport-dead check immediately
                # and retry reconnect after a longer backoff

        return {'error': f'SSH connection lost after {_MAX_RETRIES} reconnect attempts', 'exit_code': -1}

    def _tracked_exec(self, command: str, stdin_data: str, timeout: int) -> dict:
        """Run an SSH command with PID tracking for kill support.

        Wraps the command to capture the remote PID, registers with
        process_tracker, and unregisters on completion.
        """
        # We use process_tracker with a custom object that delegates to _force_stop
        class _SSHKillHandle:
            __slots__ = ('_backend',)
            def __init__(self, backend):
                self._backend = backend
            def kill(self):
                self._backend._force_stop()

        handle = _SSHKillHandle(self)
        # Use a unique placeholder PID for logging (real remote PID captured inside _exec_once)
        process_tracker.register(self._session_id, handle, 0)
        try:
            return self._exec(command, stdin_data, timeout, _track_pid=True)
        finally:
            process_tracker.unregister(self._session_id)

    def _ensure_evonic_on_remote(self):
        """Upload evonic helpers to ~/.evonic/evonic/ on first run_python call.

        Mirrors how DockerBackend mounts the helpers and LocalBackend sets
        PYTHONPATH, adapted for remote execution via SFTP.
        """
        if self._evonic_installed:
            return
        remote_dir = os.path.expanduser(_REMOTE_EVONIC_DIR)
        remote_bin = remote_dir + '/bin'
        self._exec(f'mkdir -p {shlex.quote(remote_dir)} {shlex.quote(remote_bin)}', '', 10)
        files = [
            ('__init__.py', f'{remote_dir}/__init__.py'),
            ('display.py', f'{remote_dir}/display.py'),
            ('http.py', f'{remote_dir}/http.py'),
            ('bin/rg', f'{remote_bin}/rg'),
        ]
        for rel_path, remote_path in files:
            local_path = os.path.join(_HELPERS_DIR, rel_path)
            if os.path.isfile(local_path):
                result = self.sftp_upload(local_path, remote_path)
                if 'error' in result:
                    logger.warning('[ssh_evonic] Upload failed %s: %s', rel_path, result['error'])
                    return
        self._exec(f'chmod +x {shlex.quote(remote_bin)}/rg', '', 5)
        self._evonic_installed = True
        logger.info('[ssh_evonic] Installed evonic helpers on %s', self._host)

    def run_bash(self, script: str, timeout: int, env: dict) -> dict:
        # Prepend env exports before the script
        env_prefix = ''.join(
            f"export {k}={_shell_quote(v)}\n" for k, v in env.items()
        )
        return self._tracked_exec('bash -s', env_prefix + script, timeout)

    def run_python(self, code: str, timeout: int, env: dict) -> dict:
        self._ensure_evonic_on_remote()
        remote_dir = os.path.expanduser(_REMOTE_EVONIC_DIR)
        merged = dict(env or {})
        existing = merged.get('PYTHONPATH', '')
        merged['PYTHONPATH'] = f'{remote_dir}:{existing}'.rstrip(':')
        env_prefix = ''.join(
            f"export {k}={_shell_quote(v)}\n" for k, v in merged.items()
        )
        # Wrap: set env vars in shell, then pipe code to python3
        wrapper = env_prefix + 'python3 -'
        return self._tracked_exec('bash -c ' + _shell_quote(wrapper), code, timeout)

    def file_exists(self, path: str) -> bool:
        r = self._exec(f'test -e {shlex.quote(path)} && echo yes || echo no', '', 5)
        return r.get('stdout', '').strip() == 'yes'

    def file_stat(self, path: str) -> dict:
        r = self.run_python(file_stat_code(path), 10, {})
        return parse_file_stat_output(r.get('stdout', ''))

    def read_file(self, path: str) -> dict:
        r = self._exec(f'cat {shlex.quote(path)}', '', 30)
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or r.get('error', 'read failed')}
        return {'content': r.get('stdout', '')}

    def write_file(self, path: str, content: str, create_dirs: bool = True) -> dict:
        encoded = base64.b64encode(content.encode('utf-8')).decode('ascii')
        script = ''
        if create_dirs:
            dir_path = path.rsplit('/', 1)[0] if '/' in path else ''
            if dir_path:
                script += f'mkdir -p {shlex.quote(dir_path)}\n'
        script += f'echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}\n'
        r = self._exec('bash -s', script, 30)
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or r.get('error', 'write failed')}
        return {'ok': True}

    def make_dirs(self, path: str) -> dict:
        r = self._exec(f'mkdir -p {shlex.quote(path)}', '', 10)
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or r.get('error', 'mkdir failed')}
        return {'ok': True}

    def sftp_upload(self, local_path: str, remote_path: str, progress_cb=None) -> dict:
        """Upload a local file to the remote host via SFTP (binary-safe, no size limit)."""
        try:
            remote_dir = remote_path.rsplit('/', 1)[0] if '/' in remote_path else ''
            if remote_dir:
                self._exec(f'mkdir -p {shlex.quote(remote_dir)}', '', 10)
            sftp = self._client.open_sftp()
            try:
                sftp.put(local_path, remote_path, callback=progress_cb)
                return {'ok': True}
            finally:
                sftp.close()
        except Exception as e:
            # Retry once on connection loss
            try:
                self._connect()
                sftp = self._client.open_sftp()
                try:
                    sftp.put(local_path, remote_path, callback=progress_cb)
                    return {'ok': True}
                finally:
                    sftp.close()
            except Exception as e2:
                return {'error': f'SFTP upload failed: {e2}'}

    def sftp_download(self, remote_path: str, local_path: str, progress_cb=None) -> dict:
        """Download a file from the remote host to local filesystem via SFTP (binary-safe)."""
        try:
            os.makedirs(os.path.dirname(local_path) or '.', exist_ok=True)
            sftp = self._client.open_sftp()
            try:
                sftp.get(remote_path, local_path, callback=progress_cb)
                return {'ok': True}
            finally:
                sftp.close()
        except Exception as e:
            # Retry once on connection loss
            try:
                self._connect()
                sftp = self._client.open_sftp()
                try:
                    sftp.get(remote_path, local_path, callback=progress_cb)
                    return {'ok': True}
                finally:
                    sftp.close()
            except Exception as e2:
                return {'error': f'SFTP download failed: {e2}'}

    def destroy(self) -> dict:
        try:
            self._client.close()
        except Exception:
            pass
        return {'result': 'ssh_disconnected', 'host': self._host, 'username': self._username}

    def status(self) -> dict:
        transport = self._client.get_transport()
        active = transport is not None and transport.is_active()
        return {
            'backend': 'ssh',
            'host': self._host,
            'port': self._port,
            'username': self._username,
            'connected': active,
            'connected_at': self._connected_at,
            'last_used': self._last_used,
        }


def _shell_quote(s: str) -> str:
    """Single-quote a string for safe shell injection."""
    return "'" + s.replace("'", "'\\''") + "'"
