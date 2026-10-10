"""The runtime must not inject a message into a turn that no longer exists.

A worker whose durable turn row was reaped can leave the in-process session
busy flag set.  Injecting a message then would hand it to a dead loop and drop
it.  ``_has_live_turn`` gates that decision against the durable store.
"""

from unittest.mock import MagicMock

import backend.realtime_store as rsmod
from backend.agent_runtime import agent_runtime


def _store(monkeypatch, turns=None, error=None):
    fake = MagicMock()
    if error is not None:
        fake.active_turns.side_effect = error
    else:
        fake.active_turns.return_value = turns or []
    monkeypatch.setattr(rsmod, 'realtime_store', fake)
    return fake


def test_live_running_turn_is_detected(monkeypatch):
    fake = _store(monkeypatch, turns=[{'state': 'running'}])
    assert agent_runtime._has_live_turn('agent-a', 'session-a') is True
    fake.reap_stale_turns.assert_called_once()


def test_only_queued_turns_are_not_live(monkeypatch):
    _store(monkeypatch, turns=[{'state': 'queued'}])
    assert agent_runtime._has_live_turn('agent-a', 'session-a') is False


def test_no_turn_at_all_is_not_live(monkeypatch):
    _store(monkeypatch, turns=[])
    assert agent_runtime._has_live_turn('agent-a', 'session-a') is False


def test_probe_failure_defaults_to_live(monkeypatch):
    _store(monkeypatch, error=RuntimeError('store offline'))
    assert agent_runtime._has_live_turn('agent-a', 'session-a') is True
