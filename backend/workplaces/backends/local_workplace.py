"""
LocalWorkplaceBackend — wraps LocalBackend or DockerBackend for a Workplace execution environment.

Config keys expected in workplace.config:
  workspace_path  — absolute path of the working directory
"""

from backend.tools.lib.exec_backend import ExecutionBackend


class LocalWorkplaceBackend(ExecutionBackend):
    """Executes commands in a local directory, optionally inside a Docker container."""

    def __init__(self, config: dict, sandbox_enabled: bool = False):
        self._workspace = config.get('workspace_path')
        self._sandbox = sandbox_enabled
        self._docker = None  # lazy-init for Docker so we don't spin up a container until first use

        if not sandbox_enabled:
            from backend.tools.lib.backends.local_backend import LocalBackend
            self._inner = LocalBackend(workspace=self._workspace)
        else:
            self._inner = None  # Docker backend is session-keyed; we create it on first call

    def _get_inner(self):
        if self._inner is not None:
            return self._inner
        # Docker backend: use a stable session key so the container is reused across calls
        from backend.tools.lib.backends.docker_backend import DockerBackend
        self._inner = DockerBackend(
            session_id=f'workplace-local-{id(self)}',
            agent_id='workplace',
            workspace=self._workspace,
        )
        return self._inner

    def run_bash(self, script: str, timeout: int, env: dict) -> dict:
        return self._get_inner().run_bash(script, timeout, env)

    def run_python(self, code: str, timeout: int, env: dict) -> dict:
        return self._get_inner().run_python(code, timeout, env)

    def destroy(self) -> dict:
        if self._inner is not None:
            return self._inner.destroy()
        return {'result': 'ok', 'detail': 'No backend to destroy.'}

    # ------------------------------------------------------------------
    # File I/O — delegate to the inner backend
    # ------------------------------------------------------------------

    def file_exists(self, path: str) -> bool:
        return self._get_inner().file_exists(path)

    def file_stat(self, path: str) -> dict:
        return self._get_inner().file_stat(path)

    def read_file(self, path: str) -> dict:
        return self._get_inner().read_file(path)

    def write_file(self, path: str, content: str, create_dirs: bool = True) -> dict:
        return self._get_inner().write_file(path, content, create_dirs)

    def make_dirs(self, path: str) -> dict:
        return self._get_inner().make_dirs(path)

    def status(self) -> dict:
        if self._inner is not None:
            return self._inner.status()
        return {
            'backend': 'local_workplace',
            'sandbox': self._sandbox,
            'workspace': self._workspace,
            'status': 'idle',
        }

    def file_exists(self, path: str) -> bool:
        return self._get_inner().file_exists(path)

    def file_stat(self, path: str) -> dict:
        return self._get_inner().file_stat(path)

    def read_file(self, path: str) -> dict:
        return self._get_inner().read_file(path)

    def write_file(self, path: str, content: str, create_dirs: bool = True) -> dict:
        return self._get_inner().write_file(path, content, create_dirs=create_dirs)

    def make_dirs(self, path: str) -> dict:
        return self._get_inner().make_dirs(path)
