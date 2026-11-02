"""/stop must interrupt blocking LLM/tool calls immediately."""
import threading
import time

import pytest

from backend.agent_runtime.llm_loop import _STOPPED, _run_interruptible


def test_returns_value_when_not_stopped():
    assert _run_interruptible(lambda: 42, threading.Event()) == 42


def test_propagates_exception():
    def boom():
        raise ValueError("x")
    with pytest.raises(ValueError):
        _run_interruptible(boom, threading.Event())


def test_stop_returns_immediately_from_blocking_call():
    ev = threading.Event()
    threading.Timer(0.2, ev.set).start()
    start = time.monotonic()
    assert _run_interruptible(lambda: time.sleep(30), ev) is _STOPPED
    assert time.monotonic() - start < 2
