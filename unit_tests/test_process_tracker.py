"""Regression tests for local subprocess cancellation."""

import os
import threading
import time
import uuid

from backend.tools.lib.backends.local_backend import LocalBackend
from backend.tools.lib.process_tracker import process_tracker


def _process_is_alive(pid: int) -> bool:
    """Return False for a missing process or a zombie awaiting reaping."""
    try:
        with open(f'/proc/{pid}/stat', encoding='utf-8') as stat_file:
            state = stat_file.read().split()[2]
    except FileNotFoundError:
        return False
    return state != 'Z'


def test_local_bash_stop_kills_background_child_immediately(tmp_path):
    """A user stop must kill the dedicated Bash group without a parent grace period."""
    session_id = f'process-tracker-test-{uuid.uuid4().hex}'
    child_pid_file = tmp_path / 'child.pid'
    backend = LocalBackend(session_id=session_id, workspace=str(tmp_path))
    result_holder = {}

    def run_bash():
        result_holder['result'] = backend.run_bash(
            f'sleep 30 & echo $! > {child_pid_file}; wait',
            timeout=30,
            env={},
        )

    runner = threading.Thread(target=run_bash, daemon=True)
    runner.start()
    deadline = time.monotonic() + 3
    while not child_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert child_pid_file.exists(), 'background child did not start'
    child_pid = int(child_pid_file.read_text(encoding='utf-8').strip())
    assert _process_is_alive(child_pid)

    started = time.monotonic()
    process_tracker.kill(session_id)
    runner.join(timeout=1.5)
    elapsed = time.monotonic() - started

    assert not runner.is_alive(), 'Bash leader survived immediate group cancellation'
    assert elapsed < 1.5
    assert not _process_is_alive(child_pid), 'background child survived group cancellation'
    assert result_holder['result']['error'] == 'Execution stopped by user'
    assert result_holder['result']['exit_code'] == -9
