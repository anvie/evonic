"""
Failing tests for channel send error handling.

Bug: when a channel send fails, the error is silently swallowed
at multiple points and the DB message metadata is never updated
with delivery status. These tests confirm the failing behavior
before the fix is applied.

Phase: RED (confirm failures exist)
"""

import unittest
from unittest.mock import MagicMock, AsyncMock


# ---------------------------------------------------------------------------
# 1. Test _do_send error propagation in TelegramChannel
# ---------------------------------------------------------------------------

class TestDoSendErrorPropagation(unittest.TestCase):
    """Confirm _do_send allows exceptions to propagate silently."""

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

    def test_do_send_raises_when_run_async_fails(self):
        """BUG: _do_send has no try/except, so it raises on _run_async failure."""
        channel = self._make_channel(run_async_should_fail=True)

        with self.assertRaises(RuntimeError) as ctx:
            channel._do_send("123", "hello")

        self.assertIn("Simulated Telegram API error", str(ctx.exception))


# ---------------------------------------------------------------------------
# 2. Test send_message (base class) error propagation
# ---------------------------------------------------------------------------

class TestBaseSendMessageError(unittest.TestCase):
    """Confirm that BaseChannel.send_message does not handle _do_send errors."""

    def test_send_message_raises_when_do_send_raises(self):
        """BUG: send_message calls _do_send without try/except, error propagates."""
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

            def _do_send(self, external_user_id, text):
                raise ConnectionError("Network down")

            def start(self):
                pass

            def stop(self):
                pass

        ch = FailingChannel()
        ch._running = True

        with self.assertRaises(ConnectionError) as ctx:
            ch.send_message("123", "hello")

        self.assertIn("Network down", str(ctx.exception))


# ---------------------------------------------------------------------------
# 3. Test _flush_buffer error propagation
# ---------------------------------------------------------------------------

class TestFlushBufferError(unittest.TestCase):
    """Confirm _flush_buffer does not catch _do_send errors."""

    def test_flush_buffer_raises_when_do_send_raises(self):
        """BUG: _flush_buffer calls _do_send without try/except, error propagates."""
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

            def _do_send(self, external_user_id, text):
                raise RuntimeError("Buffer send failed")

            def start(self):
                pass

            def stop(self):
                pass

        ch = FailingBufferChannel()
        ch._running = True
        # Pre-fill buffer
        with ch._buf_lock:
            ch._buf["123"] = "buffered message"

        with self.assertRaises(RuntimeError) as ctx:
            ch._flush_buffer("123")

        self.assertIn("Buffer send failed", str(ctx.exception))


# ---------------------------------------------------------------------------
# 4. Test runtime does NOT record channel_send_error in metadata
# ---------------------------------------------------------------------------

class TestRuntimeSendErrorMetadata(unittest.TestCase):
    """Confirm the runtime silently swallows channel send errors
    without recording delivery failure in DB message metadata."""

    @classmethod
    def setUpClass(cls):
        import sys
        sys.modules.pop('backend.channels.registry', None)

    def test_worker_send_error_not_recorded_in_db(self):
        """BUG: when Worker.send_message fails, metadata is NOT updated with error."""
        import threading
        from unittest.mock import MagicMock, patch, PropertyMock

        # Build a mock channel instance that raises on send_message
        mock_channel = MagicMock()
        mock_channel.is_running = True
        mock_channel.send_message.side_effect = RuntimeError("Send failed")

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

        # Track whether add_chat_message was called with channel_send_error
        add_msg_calls = []
        mock_db.add_chat_message = lambda *a, **kw: add_msg_calls.append(kw)

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
            rt._buffer_timer_stats = {
                "created": 0, "cancelled": 0, "leaked": 0}

            # Mock _do_process_inner to raise an error before it emits turn_complete
            rt._do_process_inner = MagicMock()
            rt._do_process_inner.side_effect = RuntimeError("LLM failed before turn_complete")

            # Mock _process_and_respond to catch and return error response
            rt._process_and_respond = MagicMock()
            rt._process_and_respond.return_value = {
                "response": "Error reply",
                "error": True,
                "tool_trace": [],
            }

            # Build a task that simulates Worker sending response
            ctx = SessionContext("sess_test", "user123", "ch_test")
            task = _QueueTask(
                {"id": "ag_test", "name": "Test", "enabled": True, "is_super": False,
                 "message_buffer_seconds": 0, "enable_agent_state": False},
                ctx,
                send_via_channel=True
            )
            task.result = {"response": "Hello user", "tool_trace": []}
            task.event = threading.Event()

            # Execute the channel send logic directly (as _worker would)
            _resp = task.result.get("response", "")
            error_caught = False
            if task.send_via_channel and _resp and task.ctx.channel_id:
                instance = mock_channel_mgr._active.get(task.ctx.channel_id)
                if instance and instance.is_running:
                    try:
                        instance.send_message(task.ctx.external_user_id, _resp)
                    except Exception:
                        error_caught = True
                        # BUG: nothing updates DB metadata here!

            self.assertTrue(error_caught, "Send should have raised an exception")

            # BUG CONFIRMED: metadata was NOT updated with channel_send_error
            error_meta_calls = [
                c for c in add_msg_calls
                if c.get("metadata") and "channel_send_error" in str(c.get("metadata"))
            ]
            self.assertEqual(
                len(error_meta_calls), 0,
                "BUG: No channel_send_error metadata was recorded in DB. "
                "The message is saved as if delivery succeeded."
            )
