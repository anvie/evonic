"""evomem brain-DB readers must close their sqlite handle on every path."""
import sqlite3

import pytest

from backend.agent_runtime import evomem_client


class _TrackedConn:
    closed = False

    def __init__(self):
        self.row_factory = None

    def execute(self, *a, **k):
        raise sqlite3.OperationalError("boom")

    def close(self):
        _TrackedConn.closed = True


@pytest.mark.parametrize("call", [
    lambda: evomem_client.get_kb_graph_metadata("a"),
    lambda: evomem_client.get_graph_for_viz("a"),
    lambda: evomem_client.query_kb_graph("a", "doc.md"),
])
def test_connection_closed_when_query_fails(call, tmp_path, monkeypatch):
    db = tmp_path / ".evomem.db"
    db.write_bytes(b"")
    monkeypatch.setattr(evomem_client, "_get_evomem_dir", lambda agent_id: str(tmp_path))
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: _TrackedConn())
    _TrackedConn.closed = False

    assert call() is None
    assert _TrackedConn.closed
