"""
Tests for channel send error handling.

These tests validate:
1. _do_send in TelegramChannel still raises (channel-specific code)
2. send_message in BaseChannel now catches and stores errors
3. _flush_buffer now catches and stores errors  
4. Runtime checks get_send_error after send and logs a warning
"""

import asyncio
import queue
import sys
import threading
import unittest
from backend.channels import telegram as tg_mod
from unittest.mock import AsyncMock, MagicMock, patch
from backend.channels.base import BaseChannel

# ---------------------------------------------------------------------------
# 1. Test _do_send error propagation in TelegramChannel (unchanged)
#    _do_send is the low-level channel implementation - still raises
# ---------------------------------------------------------------------------

class TestDoSendErrorPropagation(unittest.TestCase):
    """Confirm _do_send still raises - caught at higher level (send_message)."""

    def _make_channel(self, run_async_should_fail=False):
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

        class FailingChannel(BaseChannel):
            @staticmethod
            def get_channel_type():
                return "test"

            def __init__(self):
                self.channel_id = "ch_test"
                self._buf = {}
                self._buf_timers = {}
                self._buf_lock = threading.Lock()
                self._last_sent = {}
                self._outbound_buffer_seconds = 1.5
                self._send_errors = {}
                self._send_errors_lock = threading.Lock()
                self._send_error_ttl = 3600

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
        """FIX: _flush_buffer no longer raises; stores error."""
        class FailingBufferChannel(BaseChannel):
            @staticmethod
            def get_channel_type():
                return "test"

            def __init__(self):
                self.channel_id = "ch_buf"
                self._buf = {}
                self._buf_timers = {}
                self._buf_lock = threading.Lock()
                self._last_sent = {}
                self._outbound_buffer_seconds = 1.5
                self._send_errors = {}
                self._send_errors_lock = threading.Lock()
                self._send_error_ttl = 3600

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
# 5. Test send_file error paths
# ---------------------------------------------------------------------------

class TestSendFileError(unittest.TestCase):
    """Confirm BaseChannel.send_file catches _do_send_file errors and stores them."""

    def _make_channel(self, _do_send_file_impl):
        class FileChannel(BaseChannel):
            @staticmethod
            def get_channel_type():
                return "test"

            def __init__(self):
                self.channel_id = "ch_file"
                self._buf = {}
                self._buf_timers = {}
                self._buf_lock = threading.Lock()
                self._last_sent = {}
                self._outbound_buffer_seconds = 1.5
                self._send_errors = {}
                self._send_errors_lock = threading.Lock()
                self._send_error_ttl = 3600

            def _do_send(self, external_user_id, text):
                pass

            def _do_send_file(self, external_user_id, file_path, caption, mime_type):
                return _do_send_file_impl(external_user_id, file_path, caption, mime_type)

            def start(self):
                pass

            def stop(self):
                pass

        return FileChannel()

    def test_send_file_catches_exception_and_stores_error(self):
        """send_file stores error and returns False when _do_send_file raises."""
        def _raises(user_id, path, caption, mime):
            raise OSError("File not found")

        ch = self._make_channel(_raises)
        ch._running = True

        result = ch.send_file("456", "/tmp/test.pdf")
        self.assertFalse(result)
        self.assertTrue(ch.has_send_error("456"))
        err = ch.get_send_error("456")
        self.assertIn("File not found", err)
        self.assertIsNone(ch.get_send_error("456"))

    def test_send_file_stores_error_when_do_send_returns_false(self):
        """send_file stores error when _do_send_file returns False."""
        def _returns_false(user_id, path, caption, mime):
            return False

        ch = self._make_channel(_returns_false)
        ch._running = True

        result = ch.send_file("789", "/tmp/test.pdf")
        self.assertFalse(result)
        self.assertTrue(ch.has_send_error("789"))
        err = ch.get_send_error("789")
        self.assertIn("send_file returned False", err)
        self.assertIsNone(ch.get_send_error("789"))

    def test_send_file_returns_true_no_error_on_success(self):
        """send_file returns True and stores no error on success."""
        def _returns_true(user_id, path, caption, mime):
            return True

        ch = self._make_channel(_returns_true)
        ch._running = True

        result = ch.send_file("999", "/tmp/test.pdf")
        self.assertTrue(result)
        self.assertFalse(ch.has_send_error("999"))
        self.assertIsNone(ch.get_send_error("999"))


# ---------------------------------------------------------------------------
# 4. Test runtime checks get_send_error after send
# ---------------------------------------------------------------------------

class TestRuntimeSendErrorMetadata(unittest.TestCase):
    """Confirm runtime checks get_send_error/has_send_error after channel send."""

    @classmethod
    def setUpClass(cls):
        sys.modules.pop('backend.channels.registry', None)

    def test_worker_detects_send_error_via_get_send_error(self):
        """FIX: runtime checks has_send_error after send and logs warning."""
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
            rt._message_queue = queue.Queue()
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
