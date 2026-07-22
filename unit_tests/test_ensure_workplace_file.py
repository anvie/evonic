"""Tests for temporary attachment staging in execution backends."""
from pathlib import Path

from backend.tools import _ensure_workplace_file as transfer
from backend.tools._workspace import scratch_dir


class _Backend:
    def __init__(self):
        self.writes = []

    def resolve_path(self, path):
        return f"remote:{path}"

    def write_file_bytes(self, path, data, create_dirs=True):
        self.writes.append((path, data, create_dirs))
        return {'ok': True}

    def file_stat(self, path):
        return {'size': len(self.writes[-1][1])}


def test_staging_path_is_agent_and_session_scoped_and_unique():
    agent = {'id': 'agent one', 'session_id': 'session/one'}
    first = transfer._staging_path('/host/report.txt', agent)
    second = transfer._staging_path('/host/report.txt', agent)

    expected = Path(scratch_dir('agent_one')) / 'attachments' / 'session_one'
    assert Path(first).parent == expected
    assert Path(first).name.endswith('_report.txt')
    assert first != second


def test_sandbox_transfer_uses_backend_visible_scratch_path(tmp_path, monkeypatch):
    source = tmp_path / 'report.txt'
    source.write_bytes(b'attachment data')
    backend = _Backend()
    monkeypatch.setattr(
        'backend.tools.lib.exec_backend.registry.get_backend',
        lambda session_id, agent: backend,
    )
    agent = {'id': 'agent_a', 'session_id': 'session_a', 'sandbox_enabled': True}

    result = transfer.ensure_workplace_file(str(source), agent)

    assert result.startswith(f"{scratch_dir('agent_a')}/attachments/session_a/")
    assert len(backend.writes) == 1
    path, data, create_dirs = backend.writes[0]
    assert path == f'remote:{result}'
    assert data == b'attachment data'
    assert create_dirs is True


def test_local_agent_returns_original_path_without_transfer(tmp_path):
    source = tmp_path / 'report.txt'
    source.write_text('attachment data')

    assert transfer.ensure_workplace_file(str(source), {'id': 'agent_a'}) == str(source)
