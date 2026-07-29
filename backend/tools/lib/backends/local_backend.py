"""
LocalBackend — runs bash and Python directly on the host via subprocess.

Used when sandbox_enabled=0 in agent_context. No container, no isolation.
"""

import codecs
import os
import pwd
import select
import subprocess
import time

from backend.tools.lib.exec_backend import ExecutionBackend, truncate
from backend.tools.lib.process_tracker import process_tracker

try:
    from config import SANDBOX_WORKSPACE
except ImportError:
    SANDBOX_WORKSPACE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))

_MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB

# Directory containing the evonic -> runpy_helpers symlink, so that
# `from evonic import tree` works in non-sandbox (local) mode.
_HELPERS_PARENT_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..'))
_EVONIC_SYMLINK = os.path.join(_HELPERS_PARENT_DIR, 'evonic')


def _ensure_evonic_symlink():
    """Create evonic -> runpy_helpers symlink if it doesn't exist.

    In sandbox mode the runpy_helpers directory is mounted into the Docker
    container at /usr/local/lib/python3.11/site-packages/evonic/.  In local
    (non-sandbox) mode we create a symlink so the same ``from evonic import
    tree`` idiom works on the host.
    """
    if not os.path.exists(_EVONIC_SYMLINK):
        try:
            os.symlink('runpy_helpers', _EVONIC_SYMLINK)
        except OSError:
            pass  # best-effort; run_python will still set PYTHONPATH


_ensure_evonic_symlink()


class LocalBackend(ExecutionBackend):
    """Executes bash/python directly on the host (no sandboxing).

    When *run_as_user* is set, all commands and file operations are
    executed via ``sudo -u <run_as_user>`` to provide per-agent
    process-level user isolation without Docker sandbox overhead.
    """

    def __init__(self, session_id: str = '', workspace: str = None,
                 run_as_user: str = None):
        self._session_id = session_id
        self._workspace = workspace
        stripped = (run_as_user or '').strip()
        self._run_as_user = stripped if stripped else None  # strictly None or non-empty

    def _cwd(self) -> str:
        return os.path.abspath(self._workspace or SANDBOX_WORKSPACE)

    def _subprocess_identity_kwargs(self) -> dict:
        return {}

    @staticmethod
    def _poll_proc(proc, input_data: str, timeout: int, t0: float):
        """Poll a Popen process in 1s intervals, returning (stdout, stderr, reason).

        Returns (None, None, reason) if the process was killed externally or
        timed out.  *reason* is ``'timeout'`` when the deadline was exceeded,
        or ``'killed'`` when the process died from a signal during normal
        execution (e.g. killed by process_tracker or by sudo/TTY failure).
        On success, *reason* is ``None``.
        """
        deadline = t0 + timeout
        while True:
            try:
                stdout, stderr = proc.communicate(input=input_data, timeout=1)
                input_data = None
                if proc.returncode is not None and proc.returncode < 0:
                    return None, None, 'killed'
                return stdout, stderr, None
            except subprocess.TimeoutExpired:
                input_data = None
                if proc.poll() is not None:
                    if proc.returncode < 0:
                        return None, None, 'killed'
                    try:
                        stdout, stderr = proc.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        stdout, stderr = proc.communicate(timeout=2)
                    return stdout, stderr, None
                if time.time() > deadline:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
                    return None, None, 'timeout'

    def run_bash(self, script: str, timeout: int, env: dict, on_output=None) -> dict:
        run_env = dict(os.environ)
        run_env.update(env)
        if self._run_as_user is not None:
            try:
                run_env['HOME'] = pwd.getpwnam(self._run_as_user).pw_dir
            except KeyError:
                run_env['HOME'] = f'/home/{self._run_as_user}'
        t0 = time.time()
        cmd = ['sudo', '-E', '-u', self._run_as_user, 'bash', '-s'] if self._run_as_user is not None else ['bash', '-s']
        if on_output is not None:
            return self._run_bash_streaming(cmd, script, timeout, run_env, t0, on_output)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=self._cwd(), env=run_env,
            start_new_session=True,
            **self._subprocess_identity_kwargs(),
        )
        process_tracker.register(self._session_id, proc, proc.pid,
                                 kill_method='killpg_immediate')
        try:
            stdout, stderr, reason = self._poll_proc(proc, script, timeout, t0)
            if stdout is None:
                elapsed = round(time.time() - t0, 3)
                if reason == 'timeout':
                    return {
                        'error': f'Execution timed out after {timeout}s',
                        'exit_code': -1,
                        'execution_time': elapsed,
                    }
                # Process killed by signal — check if user requested the stop
                # (process_tracker.kill() unregisters before we get here)
                was_user_stop = not process_tracker.is_registered(self._session_id)
                if was_user_stop:
                    return {
                        'error': 'Execution stopped by user',
                        'exit_code': -9,
                        'execution_time': elapsed,
                    }
                sig = -proc.returncode if proc.returncode else 'unknown'
                return {
                    'error': f'Process killed by signal {sig}. This may happen when a command requires interactive input (e.g. sudo password prompt) that cannot be provided in this environment.',
                    'exit_code': proc.returncode or -9,
                    'execution_time': elapsed,
                }
        finally:
            process_tracker.unregister(self._session_id)
        elapsed = round(time.time() - t0, 3)
        return {
            'stdout': truncate(stdout, _MAX_OUTPUT_BYTES),
            'stderr': truncate(stderr, _MAX_OUTPUT_BYTES),
            'exit_code': proc.returncode,
            'execution_time': elapsed,
        }

    def _run_bash_streaming(self, cmd, script, timeout, run_env, t0, on_output) -> dict:
        """Run bash streaming combined stdout+stderr to on_output as it arrives.

        Uses select() + os.read() so the timeout is enforced even while the
        process is silent, and partial (newline-less) output is emitted without
        blocking. Returns the same dict shape as run_bash, with the full
        captured output in 'stdout' (stderr is merged into stdout while
        streaming).
        """
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=self._cwd(), env=run_env, start_new_session=True,
            **self._subprocess_identity_kwargs(),
        )
        process_tracker.register(
            self._session_id, proc, proc.pid, kill_method='killpg_immediate'
        )
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        chunks = []
        emitted = 0
        timed_out = False

        def _push(data: bytes):
            nonlocal emitted
            text = decoder.decode(data)
            if not text:
                return
            chunks.append(text)
            if emitted < _MAX_OUTPUT_BYTES:
                on_output(text)
                emitted += len(text)

        try:
            try:
                proc.stdin.write(script.encode('utf-8'))
                proc.stdin.close()
            except Exception:
                pass

            deadline = t0 + timeout
            fd = proc.stdout.fileno()
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    rlist, _, _ = select.select([fd], [], [], min(0.5, remaining))
                except (ValueError, OSError):
                    break
                if rlist:
                    try:
                        data = os.read(fd, 65536)
                    except OSError:
                        break
                    if data == b'':
                        break  # EOF
                    _push(data)
                elif proc.poll() is not None:
                    break
            # Final drain (EOF or exit): read whatever remains without blocking.
            while True:
                try:
                    rlist, _, _ = select.select([fd], [], [], 0)
                except (ValueError, OSError):
                    break
                if not rlist:
                    break
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if data == b'':
                    break
                _push(data)
        finally:
            if timed_out:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        pass
                except Exception:
                    pass
            else:
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
            process_tracker.unregister(self._session_id)

        elapsed = round(time.time() - t0, 3)
        output = truncate(''.join(chunks), _MAX_OUTPUT_BYTES)
        if timed_out:
            return {
                'error': f'Execution timed out after {timeout}s',
                'exit_code': -1,
                'stdout': output,
                'stderr': '',
                'execution_time': elapsed,
            }
        return {
            'stdout': output,
            'stderr': '',
            'exit_code': proc.returncode,
            'execution_time': elapsed,
        }

    def run_python(self, code: str, timeout: int, env: dict) -> dict:
        run_env = dict(os.environ)
        run_env.update(env)
        if self._run_as_user is not None:
            try:
                run_env['HOME'] = pwd.getpwnam(self._run_as_user).pw_dir
            except KeyError:
                run_env['HOME'] = f'/home/{self._run_as_user}'
        existing = run_env.get('PYTHONPATH', '')
        run_env['PYTHONPATH'] = f"{_HELPERS_PARENT_DIR}{os.pathsep}{existing}".rstrip(os.pathsep)
        t0 = time.time()
        cmd = ['sudo', '-E', '-u', self._run_as_user, 'python3', '-'] if self._run_as_user is not None else ['python3', '-']
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=self._cwd(), env=run_env,
            start_new_session=True,
            **self._subprocess_identity_kwargs(),
        )
        process_tracker.register(self._session_id, proc, proc.pid,
                                 kill_method='killpg')
        try:
            stdout, stderr, reason = self._poll_proc(proc, code, timeout, t0)
            if stdout is None:
                elapsed = round(time.time() - t0, 3)
                if reason == 'timeout':
                    return {
                        'error': f'Execution timed out after {timeout}s',
                        'exit_code': -1,
                        'execution_time': elapsed,
                    }
                was_user_stop = not process_tracker.is_registered(self._session_id)
                if was_user_stop:
                    return {
                        'error': 'Execution stopped by user',
                        'exit_code': -9,
                        'execution_time': elapsed,
                    }
                sig = -proc.returncode if proc.returncode else 'unknown'
                return {
                    'error': f'Process killed by signal {sig}. This may happen when a command requires interactive input (e.g. sudo password prompt) that cannot be provided in this environment.',
                    'exit_code': proc.returncode or -9,
                    'execution_time': elapsed,
                }
        finally:
            process_tracker.unregister(self._session_id)
        elapsed = round(time.time() - t0, 3)
        return {
            'stdout': truncate(stdout, _MAX_OUTPUT_BYTES),
            'stderr': truncate(stderr, _MAX_OUTPUT_BYTES),
            'exit_code': proc.returncode,
            'execution_time': elapsed,
        }

    def destroy(self) -> dict:
        return {'result': 'ok', 'detail': 'LocalBackend has no resources to destroy.'}

    def status(self) -> dict:
        info = {'backend': 'local', 'workspace': self._cwd()}
        if self._run_as_user is not None:
            info['run_as_user'] = self._run_as_user
        return info

    # ------------------------------------------------------------------
    # File I/O — direct host filesystem (same as original tool behavior).
    # When run_as_user is set, all file ops are delegated via
    # ``sudo -u <user> python3 -c "..."`` so they execute as the target
    # user instead of the Evonic server process user.
    # ------------------------------------------------------------------

    def _sudo_subprocess(self, python_code: str, timeout: int = 10) -> dict:
        """Run a short Python snippet as *run_as_user* via sudo.

        Returns ``{'ok': True, 'result': ...}`` or ``{'error': str}``.
        *result* is the decoded stdout, or the raw dict parsed from a
        JSON line printed by the snippet.
        """
        if self._run_as_user is None:
            return {'error': 'run_as_user not set'}
        try:
            proc = subprocess.run(
                ['sudo', '-u', self._run_as_user, 'python3', '-c', python_code],
                capture_output=True, text=True, timeout=timeout,
            )
            if proc.returncode != 0:
                err = proc.stderr.strip() or proc.stdout.strip() or 'unknown error'
                return {'error': err}
            stdout = proc.stdout.strip()
            # Try JSON parse; if not valid JSON, return as plain string
            if stdout:
                try:
                    import json
                    return {'ok': True, 'result': json.loads(stdout)}
                except (json.JSONDecodeError, ValueError):
                    return {'ok': True, 'result': stdout}
            return {'ok': True, 'result': ''}
        except subprocess.TimeoutExpired:
            return {'error': 'sudo file operation timed out'}
        except FileNotFoundError:
            return {'error': 'sudo command not found on this system'}
        except Exception as e:
            return {'error': str(e)}

    def file_exists(self, path: str) -> bool:
        if self._run_as_user is None:
            return os.path.exists(path)
        code = (
            "import os, json; "
            "print(json.dumps(os.path.exists(" + repr(path) + ")))"
        )
        r = self._sudo_subprocess(code)
        if r.get('ok'):
            return bool(r.get('result'))
        return False

    def file_stat(self, path: str) -> dict:
        if self._run_as_user is None:
            if not os.path.exists(path):
                return {'exists': False}
            size = os.path.getsize(path)
            is_binary = False
            if size > 0:
                try:
                    with open(path, 'rb') as f:
                        is_binary = b'\x00' in f.read(8192)
                except Exception:
                    pass
            return {'exists': True, 'size': size, 'is_binary': is_binary}
        code = (
            "import os, json; p=" + repr(path) + "; "
            "print(json.dumps({'exists': os.path.exists(p), "
            "'size': os.path.getsize(p) if os.path.isfile(p) else 0, "
            "'is_binary': (lambda: (__import__('os').path.getsize(p) > 0 and "
            "b'\\x00' in open(p, 'rb').read(8192)) if "
            "__import__('os').path.isfile(p) else False)()}))"
        )
        r = self._sudo_subprocess(code)
        if r.get('ok') and isinstance(r.get('result'), dict):
            return r['result']
        return {'exists': False, 'size': 0, 'is_binary': False}

    def read_file(self, path: str) -> dict:
        if self._run_as_user is None:
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    return {'content': f.read()}
            except PermissionError:
                return {'error': 'Permission denied — cannot read this file.'}
            except IsADirectoryError:
                return {'error': f'Path is a directory, not a file: {path}'}
            except UnicodeDecodeError:
                return {'error': 'File contains non-UTF-8 characters.'}
            except Exception as e:
                return {'error': str(e)}
        # Content is base64-encoded with a __B64__ marker so it survives
        # _sudo_subprocess's stdout.strip() + json.loads() untouched
        # (JSON-looking files, leading/trailing whitespace, print's newline).
        import base64
        code = (
            "import base64\n"
            "p=" + repr(path) + "\n"
            "try:\n"
            " f=open(p,'r',encoding='utf-8',errors='replace')\n"
            " print('__B64__'+base64.b64encode(f.read().encode('utf-8')).decode('ascii')); f.close()\n"
            "except PermissionError: print('__ERR__Permission denied')\n"
            "except IsADirectoryError: print('__ERR__Path is a directory')\n"
            "except UnicodeDecodeError: print('__ERR__Non-UTF-8 file')\n"
            "except Exception as e: print('__ERR__'+str(e))"
        )
        r = self._sudo_subprocess(code)
        if r.get('ok'):
            result = r.get('result', '')
            if isinstance(result, str) and result.startswith('__ERR__'):
                return {'error': result[7:]}
            if isinstance(result, str) and result.startswith('__B64__'):
                return {'content': base64.b64decode(result[7:]).decode('utf-8', errors='replace')}
            return {'error': 'Unexpected result format'}
        return {'error': r.get('error', 'Unknown error')}

    def write_file(self, path: str, content: str, create_dirs: bool = True) -> dict:
        if self._run_as_user is None:
            try:
                if create_dirs:
                    parent = os.path.dirname(path)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                return {'ok': True}
            except PermissionError:
                return {'error': f'Permission denied writing: {path}'}
            except IsADirectoryError:
                return {'error': f'Path is a directory, not a file: {path}'}
            except Exception as e:
                return {'error': str(e)}
        import base64
        encoded = base64.b64encode(content.encode('utf-8')).decode('ascii')
        code = (
            "import os, base64\n"
            "p=" + repr(path) + "\n"
            "cd=" + repr(create_dirs) + "\n"
            "data=base64.b64decode(" + repr(encoded) + ").decode('utf-8')\n"
            "try:\n"
            " if cd:\n"
            "  d=os.path.dirname(p)\n"
            "  if d: os.makedirs(d,exist_ok=True)\n"
            " f=open(p,'w',encoding='utf-8'); f.write(data); f.close()\n"
            " print('__OK__')\n"
            "except PermissionError: print('__ERR__Permission denied')\n"
            "except IsADirectoryError: print('__ERR__Path is a directory')\n"
            "except Exception as e: print('__ERR__'+str(e))"
        )
        r = self._sudo_subprocess(code)
        if r.get('ok'):
            result = r.get('result', '')
            if isinstance(result, str) and result.startswith('__ERR__'):
                return {'error': result[7:]}
            return {'ok': True}
        return {'error': r.get('error', 'Unknown error')}

    def cat_file_bytes(self, path: str) -> dict:
        """Read a file as raw bytes directly from the host filesystem."""
        if self._run_as_user is None:
            try:
                with open(path, 'rb') as f:
                    return {'bytes': f.read()}
            except PermissionError:
                return {'error': 'Permission denied — cannot read this file.'}
            except FileNotFoundError:
                return {'error': f'File not found: {path}'}
            except IsADirectoryError:
                return {'error': f'Path is a directory, not a file: {path}'}
            except Exception as e:
                return {'error': str(e)}
        import base64
        code = (
            "import base64\n"
            "p=" + repr(path) + "\n"
            "try:\n"
            " data=open(p,'rb').read()\n"
            " print('__B64__'+base64.b64encode(data).decode('ascii'))\n"
            "except PermissionError: print('__ERR__Permission denied')\n"
            "except FileNotFoundError: print('__ERR__File not found')\n"
            "except IsADirectoryError: print('__ERR__Path is a directory')\n"
            "except Exception as e: print('__ERR__'+str(e))"
        )
        r = self._sudo_subprocess(code)
        if r.get('ok'):
            result = r.get('result', '')
            if isinstance(result, str):
                if result.startswith('__ERR__'):
                    return {'error': result[7:]}
                if result.startswith('__B64__'):
                    return {'bytes': base64.b64decode(result[7:])}
            return {'error': 'Unexpected result format'}
        return {'error': r.get('error', 'Unknown error')}

    def delete_file(self, path: str) -> dict:
        """Delete a file from the host filesystem."""
        if self._run_as_user is None:
            try:
                os.remove(path)
                return {'ok': True}
            except FileNotFoundError:
                return {'error': f'File not found: {path}'}
            except PermissionError:
                return {'error': f'Permission denied: {path}'}
            except IsADirectoryError:
                return {'error': f'Path is a directory, not a file: {path}'}
            except Exception as e:
                return {'error': str(e)}
        code = (
            "import os\n"
            "p=" + repr(path) + "\n"
            "try:\n"
            " os.remove(p); print('__OK__')\n"
            "except FileNotFoundError: print('__ERR__File not found')\n"
            "except PermissionError: print('__ERR__Permission denied')\n"
            "except IsADirectoryError: print('__ERR__Path is a directory')\n"
            "except Exception as e: print('__ERR__'+str(e))"
        )
        r = self._sudo_subprocess(code)
        if r.get('ok'):
            result = r.get('result', '')
            if isinstance(result, str) and result.startswith('__ERR__'):
                return {'error': result[7:]}
            return {'ok': True}
        return {'error': r.get('error', 'Unknown error')}

    def write_file_bytes(self, path: str, data: bytes, create_dirs: bool = True) -> dict:
        if self._run_as_user is None:
            try:
                if create_dirs:
                    parent = os.path.dirname(path)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                with open(path, 'wb') as f:
                    f.write(data)
                return {'ok': True}
            except PermissionError:
                return {'error': f'Permission denied writing: {path}'}
            except IsADirectoryError:
                return {'error': f'Path is a directory, not a file: {path}'}
            except Exception as e:
                return {'error': str(e)}
        import base64
        encoded = base64.b64encode(data).decode('ascii')
        code = (
            "import os, base64\n"
            "p=" + repr(path) + "\n"
            "cd=" + repr(create_dirs) + "\n"
            "data=base64.b64decode(" + repr(encoded) + ")\n"
            "try:\n"
            " if cd:\n"
            "  d=os.path.dirname(p)\n"
            "  if d: os.makedirs(d,exist_ok=True)\n"
            " f=open(p,'wb'); f.write(data); f.close()\n"
            " print('__OK__')\n"
            "except PermissionError: print('__ERR__Permission denied')\n"
            "except IsADirectoryError: print('__ERR__Path is a directory')\n"
            "except Exception as e: print('__ERR__'+str(e))"
        )
        r = self._sudo_subprocess(code)
        if r.get('ok'):
            result = r.get('result', '')
            if isinstance(result, str) and result.startswith('__ERR__'):
                return {'error': result[7:]}
            return {'ok': True}
        return {'error': r.get('error', 'Unknown error')}

    def make_dirs(self, path: str) -> dict:
        if self._run_as_user is None:
            try:
                os.makedirs(path, exist_ok=True)
                return {'ok': True}
            except Exception as e:
                return {'error': str(e)}
        code = (
            "import os\n"
            "p=" + repr(path) + "\n"
            "try:\n"
            " os.makedirs(p,exist_ok=True); print('__OK__')\n"
            "except Exception as e: print('__ERR__'+str(e))"
        )
        r = self._sudo_subprocess(code)
        if r.get('ok'):
            result = r.get('result', '')
            if isinstance(result, str) and result.startswith('__ERR__'):
                return {'error': result[7:]}
            return {'ok': True}
        return {'error': r.get('error', 'Unknown error')}
