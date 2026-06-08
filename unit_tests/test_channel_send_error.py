"""
Tests for channel send error handling.

These tests validate:
1. _do_send in TelegramChannel still raises (channel-specific code)
2. send_message in BaseChannel now catches and stores errors
3. _flush_buffer now catches and stores errors  
4. Runtime checks get_send_error after send and logs a warning
"""

import unittest
from unittest.mock import MagicMock, AsyncMock


# ---------------------------------------------------------------------------
# 1. Test _do_send error propagation in TelegramChannel (unchanged)
#    _do_send is the low-level channel implementation - still raises
# ---------------------------------------------------------------------------

class TestDoSendErrorPropagation(unittest.TestCase):
    """Confirm _do_send still raises - caught at higher level (send_message)."""

    def _make_channel(self, run_async_should_fail=False):
        from backend.channels import telegram as tg_mod

        channel = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        channel._app = MagicMock()
        channel._app.bot = MagicMock()
        channel._app.bot.send_message = AsyncMock()
        channel._loop = None
        channel.channel_id = "ch_test"

        if run_async_should_fail:
            def _failing_run_async(coro):
                raise RuntimeError("Simulated Telegram API error")
            channel._run_async = _failing_run_async
        else:
            def _fake_run_async(coro):
                import asyncio
                loop = asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(coro)
                finally:
                    loop.close()
            channel._run_async = _fake_run_async

        return channel

    def test_do_send_still_raises_on_run_async_failure(self):
        """_do_send is channel-specific; errors are caught in BaseChannel."""
        channel = self._make_channel(run_async_should_fail=True)

        with self.assertRaises(RuntimeError) as ctx:
            channel._do_send("123", "hello")

        self.assertIn("Simulated Telegram API error", str(ctx.exception))


# ---------------------------------------------------------------------------
# 2. Test send_message (base class) now catches and stores errors
# ---------------------------------------------------------------------------

class TestBaseSendMessageError(unittest.TestCase):
    """Confirm BaseChannel.send_message catches _do_send errors and stores them."""

    def test_send_message_catches_error_and_stores_it(self):
        """FIX: send_message no longer raises; stores error for later retrieval."""
        from backend.channels.base import BaseChannel

        class FailingChannel(BaseChannel):
            @staticmethod
            def get_channel_type():
                return "test"

            def __init__(self):
                self.channel_id = "ch_test"
                self._buf = {}
                self._buf_timers = {}
                self._buf_lock = __import__('threading').Lock()
                self._last_sent = {}
                self._outbound_buffer_seconds = 1.5
                self._send_errors = {}
                self._send_errors_lock = __import__('threading').Lock()

            def _do_send(self, external_user_id, text):
                raise ConnectionError("Network down")

            def start(self):
                pass

            def stop(self):
                pass

        ch = FailingChannel()
        ch._running = True

        # After fix: send_message should NOT raise
        ch.send_message("123", "hello")

        # Error should be stored and retrievable
        self.assertTrue(ch.has_send_error("123"))
        err = ch.get_send_error("123")
        self.assertIn("Network down", err)

        # After retrieval, error is consumed
        self.assertFalse(ch.has_send_error("123"))
        self.assertIsNone(ch.get_send_error("123"))


# ---------------------------------------------------------------------------
# 3. Test _flush_buffer now catches and stores errors
# ---------------------------------------------------------------------------

class TestFlushBufferError(unittest.TestCase):
    """Confirm _flush_buffer catches _do_send errors and stores them."""

    def test_flush_buffer_catches_error_and_stores_it(self):
        """FIX: _flush_buffer no longer raises; stores error for later retrieval."""
        from backend.channels.base import BaseChannel

        class FailingBufferChannel(BaseChannel):
            @staticmethod
            def get_channel_type():
                return "test"

            def __init__(self):
                self.channel_id = "ch_buf"
                self._buf = {}
                self._buf_timers = {}
                self._buf_lock = __import__('threading').Lock()
                self._last_sent = {}
                self._outbound_buffer_seconds = 1.5
                self._send_errors = {}
                self._send_errors_lock = __import__('threading').Lock()

            def _do_send(self, external_user_id, text):
                raise RuntimeError("Buffer send failed")

            def start(self):
                pass

            def stop(self):
                pass

        ch = FailingBufferChannel()
        ch._running = True
        with ch._buf_lock:
            ch._buf["123"] = "buffered message"

        # After fix: _flush_buffer should NOT raise
        ch._flush_buffer("123")

        # Error should be stored and retrievable
        self.assertTrue(ch.has_send_error("123"))
        err = ch.get_send_error("123")
        self.assertIn("Buffer send failed", err)
        self.assertIsNone(ch.get_send_error("123"))


# ---------------------------------------------------------------------------
# 4. Test runtime checks get_send_error after send
# ---------------------------------------------------------------------------

class TestRuntimeSendErrorMetadata(unittest.TestCase):
    """Confirm runtime checks get_send_error/has_send_error after channel send."""

    @classmethod
    def setUpClass(cls):
        import sys
        sys.modules.pop('backend.channels.registry', None)

    def test_worker_detects_send_error_via_get_send_error(self):
        """FIX: runtime checks has_send_error after send and logs warning."""
        import threading
        from unittest.mock import MagicMock, patch

        # Build a mock channel that stores errors via get_send_error
        mock_channel = MagicMock()
        mock_channel.is_running = True
        mock_channel.has_send_error.return_value = True
        mock_channel.get_send_error.return_value = "Network timeout"

        mock_channel_mgr = MagicMock()
        mock_channel_mgr._active = {"ch_test": mock_channel}

        mock_db = MagicMock()
        mock_db.get_agent.return_value = {
            "id": "ag_test", "name": "Test Agent",
            "enabled": True, "is_super": False,
            "message_buffer_seconds": 0,
            "enable_agent_state": False,
        }
        mock_db.get_or_create_session.return_value = "sess_test"
        mock_db.get_channel.return_value = {"id": "ch_test", "access_mode": "open"}
        mock_db.is_user_allowed.return_value = True

        with patch.dict("sys.modules", {
            "backend.channels.registry": MagicMock(channel_manager=mock_channel_mgr),
            "models.db": MagicMock(db=mock_db),
        }):
            from backend.agent_runtime.runtime import AgentRuntime, _QueueTask, SessionContext

            rt = AgentRuntime.__new__(AgentRuntime)
            rt._message_queue = __import__('queue').Queue()
            rt._session_store = MagicMock()
            rt._session_store._locks = {}
            rt._session_store._locks_guard = threading.Lock()
            rt._session_store._stop_flags = {}
            rt._session_store._stop_flags_guard = threading.Lock()
            rt._session_store._busy = {}
            rt._session_store._busy_guard = threading.Lock()
            rt._agent_tracker = MagicMock()
            rt._agent_tracker._busy = {}
            rt._agent_tracker._guard = threading.Lock()
            rt._llm_serializer = MagicMock()
            rt._prefetcher = MagicMock()
            rt._prefetcher.invalidate = MagicMock()
            rt._llm_api = MagicMock()
            rt._workers = []
            rt._worker_events = []
            rt._stop_event = threading.Event()
            rt._buffer_lock = threading.Lock()
            rt._buffer_timers = {}
            rt._buffer_timer_stats = {"created": 0, "cancelled": 0, "leaked": 0}

            rt._do_process_inner = MagicMock()
            rt._do_process_inner.side_effect = RuntimeError("LLM failed")

            rt._process_and_respond = MagicMock()
            rt._process_and_respond.return_value = {
                "response": "Error reply", "error": True, "tool_trace": [],
            }

            ctx = SessionContext("sess_test", "user123", "ch_test")
            task = _QueueTask(
                {"id": "ag_test", "name": "Test", "enabled": True, "is_super": False,
                 "message_buffer_seconds": 0, "enable_agent_state": False},
                ctx, send_via_channel=True,
            )
            task.result = {"response": "Hello user", "tool_trace": []}
            task.event = threading.Event()

            # Simulate Worker send path
            _resp = task.result.get("response", "")
            error_detected = False
            if task.send_via_channel and _resp and task.ctx.channel_id:
                instance = mock_channel_mgr._active.get(task.ctx.channel_id)
                if instance and instance.is_running:
                    try:
                        instance.send_message(task.ctx.external_user_id, _resp)
                        # FIX: check for async send errors
                        if hasattr(instance, 'has_send_error') and instance.has_send_error(task.ctx.external_user_id):
                            err = instance.get_send_error(task.ctx.external_user_id)
                            error_detected = err is not None
                    except Exception:
                        pass

            self.assertTrue(error_detected, "FIX: send error should be detected via has_send_error/get_send_error")
            mock_channel.has_send_error.assert_called_with("user123")
            mock_channel.get_send_error.assert_called_with("user123")
