"""Tests for temporary attachment staging in execution backends."""
from pathlib import Path

from backend.tools import _ensure_workplace_file as transfer
from backend.tools._workspace import scratch_dir


class _Backend:
    def __init__(self, remote_prefix='remote:', existing=None):
        self.writes = []
        self._remote_prefix = remote_prefix
        self._sizes = dict(existing or {})

    def resolve_path(self, path):
        return f'{self._remote_prefix}{path}'

    def write_file_bytes(self, path, data, create_dirs=True):
        self.writes.append((path, data, create_dirs))
        self._sizes[path] = len(data)
        return {'ok': True}

    def file_stat(self, path):
        if path in self._sizes:
            return {'exists': True, 'size': self._sizes[path]}
        return {'exists': False}


class _DockerLikeBackend(_Backend):
    """Mirrors DockerBackend.resolve_path: host workspace mount -> /workspace."""

    def __init__(self, host_workspace):
        super().__init__(remote_prefix='')
        self._host_workspace = host_workspace.rstrip('/')

    def resolve_path(self, path):
        if path == self._host_workspace or path.startswith(self._host_workspace + '/'):
            return '/workspace' + path[len(self._host_workspace):]
        return path


def test_staging_path_is_agent_and_session_scoped():
    agent = {'id': 'agent one', 'session_id': 'session/one'}
    staged = transfer._staging_path('/host/report.txt', agent)

    expected = Path(scratch_dir('agent_one')) / 'attachments' / 'session_one'
    assert Path(staged).parent == expected
    assert Path(staged).name.endswith('_report.txt')


def test_staging_path_is_deterministic_for_same_file(tmp_path):
    source = tmp_path / 'report.txt'
    source.write_bytes(b'attachment data')
    agent = {'id': 'agent_a', 'session_id': 'session_a'}

    assert transfer._staging_path(str(source), agent) == \
        transfer._staging_path(str(source), agent)


def test_staging_path_changes_with_file_content(tmp_path):
    source = tmp_path / 'report.txt'
    source.write_bytes(b'one')
    agent = {'id': 'agent_a', 'session_id': 'session_a'}
    first = transfer._staging_path(str(source), agent)

    source.write_bytes(b'two-two')

    assert transfer._staging_path(str(source), agent) != first


def test_sandbox_transfer_returns_backend_visible_path(tmp_path, monkeypatch):
    source = tmp_path / 'report.txt'
    source.write_bytes(b'attachment data')
    backend = _Backend()
    monkeypatch.setattr(
        'backend.tools.lib.exec_backend.registry.get_backend',
        lambda session_id, agent: backend,
    )
    agent = {'id': 'agent_a', 'session_id': 'session_a', 'sandbox_enabled': True}

    result = transfer.ensure_workplace_file(str(source), agent)

    host_scratch = f"{scratch_dir('agent_a')}/attachments/session_a/"
    assert result.startswith(f'remote:{host_scratch}')
    assert result.startswith('remote:')
    assert len(backend.writes) == 1
    path, data, create_dirs = backend.writes[0]
    # The written path must be the returned path, not the host staging path
    assert path == result
    assert data == b'attachment data'
    assert create_dirs is True


def test_repeated_transfer_reuses_path_and_skips_upload(tmp_path, monkeypatch):
    source = tmp_path / 'report.txt'
    source.write_bytes(b'attachment data')
    backend = _Backend()
    monkeypatch.setattr(
        'backend.tools.lib.exec_backend.registry.get_backend',
        lambda session_id, agent: backend,
    )
    agent = {'id': 'agent_a', 'session_id': 'session_a', 'sandbox_enabled': True}

    first = transfer.ensure_workplace_file(str(source), agent)
    second = transfer.ensure_workplace_file(str(source), agent)

    assert first == second
    assert len(backend.writes) == 1


def test_docker_workspace_scratch_path_does_not_leak_host_path(tmp_path, monkeypatch):
    """Regression: workspace=/tmp used to return /tmp/... while writing /workspace/..."""
    source = tmp_path / 'probe.tar.gz'
    source.write_bytes(b'probe')
    backend = _DockerLikeBackend('/tmp')
    monkeypatch.setattr(
        'backend.tools.lib.exec_backend.registry.get_backend',
        lambda session_id, agent: backend,
    )
    agent = {'id': 'asep', 'session_id': 'asep-030df24d',
             'sandbox_enabled': True, 'workspace': '/tmp/'}

    result = transfer.ensure_workplace_file(str(source), agent)

    assert result.startswith('/workspace/evonic-asep-scratchpad/attachments/asep-030df24d/')
    assert backend.writes[0][0] == result


def test_local_agent_returns_original_path_without_transfer(tmp_path):
    source = tmp_path / 'report.txt'
    source.write_text('attachment data')

    assert transfer.ensure_workplace_file(str(source), {'id': 'agent_a'}) == str(source)

