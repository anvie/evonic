"""Regression tests for bounded parallel-tool future collection."""

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

# Reuse the recovery suite's lightweight agent_runtime import shim. Importing
# backend.agent_runtime normally starts global queue workers and channel services.
from unit_tests.test_llm_loop_recovery import _llm_loop_mod as llm_loop


def _job(pool, index, function, timeout):
    future = pool.submit(function)
    return index, (future, time.monotonic() + timeout)


def _wait_for_no_parallel_threads(timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(t.name.startswith('tool-parallel') for t in threading.enumerate()):
            return
        time.sleep(0.01)
    assert not any(t.name.startswith('tool-parallel') for t in threading.enumerate())


def test_hung_tool_times_out_without_waiting_for_worker():
    release = threading.Event()
    started = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tool-parallel')

    def blocked():
        started.set()
        release.wait()
        return {'content': 'late'}

    try:
        index, job = _job(pool, 0, blocked, 0.12)
        assert started.wait(0.5)
        began = time.monotonic()
        with patch.object(llm_loop, 'AGENT_PARALLEL_TOOL_WAIT_TIMEOUT', 0.12):
            results = llm_loop._collect_parallel_tool_results(
                {index: job}, pool, threading.Event())
        elapsed = time.monotonic() - began

        assert elapsed < 0.5
        assert 'timed out' in results[0]['error'].lower()
        assert not release.is_set(), 'collector must not wait for a running worker'
    finally:
        release.set()
        _wait_for_no_parallel_threads()


def test_stop_interrupts_polling_and_pairs_each_unfinished_call_once():
    release = threading.Event()
    started = threading.Event()
    stop_event = threading.Event()
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='tool-parallel')

    def blocked():
        started.set()
        release.wait()
        return {'content': 'late'}

    jobs = dict([
        _job(pool, 0, blocked, 5.0),
        _job(pool, 1, blocked, 5.0),
    ])
    timer = threading.Timer(0.08, stop_event.set)
    timer.start()
    try:
        assert started.wait(0.5)
        began = time.monotonic()
        results = llm_loop._collect_parallel_tool_results(jobs, pool, stop_event)
        elapsed = time.monotonic() - began

        assert elapsed < 0.5
        assert list(sorted(results)) == [0, 1]
        assert [results[i] for i in sorted(results)] == [
            {'error': 'Execution stopped by user'},
            {'error': 'Execution stopped by user'},
        ]
    finally:
        timer.cancel()
        release.set()
        _wait_for_no_parallel_threads()


def test_results_are_available_in_original_call_order_when_completion_differs():
    release_first = threading.Event()
    second_done = threading.Event()
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='tool-parallel')

    def first():
        release_first.wait()
        return {'content': 'first'}

    def second():
        second_done.set()
        return {'content': 'second'}

    jobs = dict([
        _job(pool, 0, first, 1.0),
        _job(pool, 1, second, 1.0),
    ])
    try:
        assert second_done.wait(0.5)
        release_first.set()
        results = llm_loop._collect_parallel_tool_results(
            jobs, pool, threading.Event())
        assert [results[i]['content'] for i in sorted(results)] == ['first', 'second']
    finally:
        release_first.set()
        _wait_for_no_parallel_threads()


def test_timeout_cancels_queued_future_and_shutdown_is_non_blocking():
    release = threading.Event()
    started = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tool-parallel')

    def blocked():
        started.set()
        release.wait()
        return {'content': 'late'}

    first_index, first_job = _job(pool, 0, blocked, 0.12)
    second_ran = threading.Event()
    second_index, second_job = _job(
        pool, 1, lambda: second_ran.set() or {'content': 'unexpected'}, 0.12)
    queued_future = second_job[0]
    try:
        assert started.wait(0.5)
        began = time.monotonic()
        with patch.object(llm_loop, 'AGENT_PARALLEL_TOOL_WAIT_TIMEOUT', 0.12):
            results = llm_loop._collect_parallel_tool_results(
                {first_index: first_job, second_index: second_job},
                pool,
                threading.Event(),
            )
        assert time.monotonic() - began < 0.5
        assert queued_future.cancelled()
        assert not second_ran.is_set()
        assert all('timed out' in results[i]['error'].lower() for i in (0, 1))
    finally:
        release.set()
        _wait_for_no_parallel_threads()


def test_worker_exception_becomes_safe_synthetic_result():
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tool-parallel')

    def fail():
        raise RuntimeError('private backend detail')

    index, job = _job(pool, 0, fail, 1.0)
    results = llm_loop._collect_parallel_tool_results(
        {index: job}, pool, threading.Event())

    assert results == {
        0: {'error': 'Parallel tool execution failed while retrieving its result.'}}
    assert 'private backend detail' not in results[0]['error']
    _wait_for_no_parallel_threads()


def test_successful_parallel_tools_remain_concurrent():
    release = threading.Event()
    both_started = threading.Barrier(3)
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='tool-parallel')

    def worker(value):
        both_started.wait(timeout=0.5)
        release.wait()
        return {'content': value}

    jobs = dict([
        _job(pool, 0, lambda: worker('a'), 1.0),
        _job(pool, 1, lambda: worker('b'), 1.0),
    ])
    collector_result = queue.Queue()
    collector = threading.Thread(
        target=lambda: collector_result.put(
            llm_loop._collect_parallel_tool_results(
                jobs, pool, threading.Event())),
        daemon=True,
    )
    collector.start()
    try:
        both_started.wait(timeout=0.5)
        release.set()
        collector.join(timeout=0.5)
        assert not collector.is_alive()
        results = collector_result.get_nowait()
        assert [results[i]['content'] for i in sorted(results)] == ['a', 'b']
    finally:
        release.set()
        collector.join(timeout=0.5)
        _wait_for_no_parallel_threads()


def test_queued_message_can_be_processed_immediately_after_timeout_recovery():
    """A timed-out batch must return control so queued input is no longer stranded."""
    release = threading.Event()
    inbox = queue.Queue()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tool-parallel')
    index, job = _job(pool, 0, lambda: release.wait() or {}, 0.1)
    inbox.put({'role': 'user', 'content': 'follow-up'})

    try:
        with patch.object(llm_loop, 'AGENT_PARALLEL_TOOL_WAIT_TIMEOUT', 0.1):
            results = llm_loop._collect_parallel_tool_results(
                {index: job}, pool, threading.Event())
        assert 'timed out' in results[0]['error'].lower()
        assert inbox.get_nowait()['content'] == 'follow-up'
    finally:
        release.set()
        _wait_for_no_parallel_threads()


def test_configured_timeout_is_bounded():
    from config import AGENT_PARALLEL_TOOL_WAIT_TIMEOUT

    assert isinstance(AGENT_PARALLEL_TOOL_WAIT_TIMEOUT, int)
    assert 1 <= AGENT_PARALLEL_TOOL_WAIT_TIMEOUT <= 3600


def test_later_future_uses_submission_deadline_not_fresh_collection_timeout():
    release = threading.Event()
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='tool-parallel')

    jobs = dict([
        _job(pool, 0, lambda: release.wait() or {'content': 'late-a'}, 0.1),
        _job(pool, 1, lambda: release.wait() or {'content': 'late-b'}, 0.1),
    ])
    try:
        began = time.monotonic()
        with patch.object(llm_loop, 'AGENT_PARALLEL_TOOL_WAIT_TIMEOUT', 0.1):
            results = llm_loop._collect_parallel_tool_results(
                jobs, pool, threading.Event())
        assert time.monotonic() - began < 0.18
        assert all('timed out' in results[i]['error'].lower() for i in (0, 1))
    finally:
        release.set()
        _wait_for_no_parallel_threads()
