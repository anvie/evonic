"""BaseChannel.deliver and AgentRuntime.deliver_as_bot say what became of a send: sent, queued, failed, or unknown (it
timed out after it was handed over and may have arrived, so it must not be sent again unasked)."""

import concurrent.futures
import threading
import unittest
from unittest.mock import MagicMock, patch

from backend.channels.base import BaseChannel


class _Channel(BaseChannel):
    @staticmethod
    def get_channel_type():
        return "test"

    def __init__(self, error=None, later=False):
        self.channel_id = "ch_test"
        self._buf, self._buf_timers, self._last_sent = {}, {}, {}
        self._buf_lock = threading.Lock()
        self._outbound_buffer_seconds = 0
        self._send_errors = {}
        self._send_errors_lock = threading.Lock()
        self._send_error_ttl = 3600
        self._running = True
        self.error, self.later, self.sent = error, later, []

    def _do_send(self, external_user_id, text, session_id=None):
        if self.error:
            raise self.error
        self.sent.append(text)

    def sends_later(self):
        return self.later

    def start(self):
        pass

    def stop(self):
        pass


class TestDeliver(unittest.TestCase):
    def test_a_send_that_went_out_is_sent(self):
        ch = _Channel()
        self.assertEqual(ch.deliver("1", "hi"), {'status': 'sent', 'error': None})
        self.assertEqual(ch.sent, ["hi"])

    def test_a_refused_send_is_failed_with_its_reason(self):
        out = _Channel(ConnectionError("Network down")).deliver("1", "hi")
        self.assertEqual(out['status'], 'failed')
        self.assertIn("Network down", out['error'])

    def test_a_timeout_is_unknown_and_named_though_its_message_is_empty(self):
        for exc in (concurrent.futures.TimeoutError(), TimeoutError()):
            out = _Channel(exc).deliver("1", "hi")
            self.assertEqual(out['status'], 'unknown')
            self.assertTrue(out['error'])                       # '' would read as no error at all

    def test_a_library_timeout_class_is_unknown_too(self):
        class TimedOut(Exception):                              # as python-telegram-bot names it
            pass
        self.assertEqual(_Channel(TimedOut("Timed out")).deliver("1", "hi")['status'], 'unknown')

    def test_an_earlier_failure_does_not_answer_for_this_send(self):
        ch = _Channel()
        ch._store_send_error("1", "an old failure")
        self.assertEqual(ch.deliver("1", "hi")['status'], 'sent')

    def test_a_channel_that_sends_from_a_queue_says_queued(self):
        self.assertEqual(_Channel(later=True).deliver("1", "hi")['status'], 'queued')

    def test_a_stopped_channel_sends_nothing(self):
        ch = _Channel()
        ch._running = False
        self.assertEqual(ch.deliver("1", "hi")['status'], 'failed')
        self.assertEqual(ch.sent, [])

    def test_the_old_readers_still_work(self):
        ch = _Channel(ConnectionError("down"))
        ch.send_message("1", "hi")
        self.assertTrue(ch.has_send_error("1"))
        self.assertEqual(ch.get_send_error("1"), "down")


class TestDeliverAsBot(unittest.TestCase):
    def _runtime(self, session, channel):
        from backend.agent_runtime import runtime as rt
        runtime = rt.AgentRuntime.__new__(rt.AgentRuntime)
        patches = [patch.object(rt.db, 'get_session_with_details', return_value=session),
                   patch.object(rt.db, 'add_chat_message', return_value=1),
                   patch.object(rt, 'chatlog_manager', MagicMock()),
                   patch.object(rt, 'event_stream', MagicMock()),
                   patch.object(rt.channel_manager, '_active', {'ch_test': channel} if channel else {})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return runtime

    SESSION = {'agent_id': 'a', 'external_user_id': '1', 'channel_id': 'ch_test'}

    def test_a_failed_channel_send_is_reported_but_the_old_bool_stays_true(self):
        runtime = self._runtime(self.SESSION, _Channel(ConnectionError("down")))
        out = runtime.deliver_as_bot('s1', 'hi')
        self.assertEqual((out['saved'], out['status']), (True, 'failed'))
        self.assertTrue(runtime.send_as_bot('s1', 'hi'))       # the admin takeover route keeps its meaning

    def test_a_channel_that_is_not_running_is_failed_not_sent(self):
        out = self._runtime(self.SESSION, None).deliver_as_bot('s1', 'hi')
        self.assertEqual(out['status'], 'failed')

    def test_a_session_with_no_channel_is_the_web_chat(self):
        out = self._runtime({**self.SESSION, 'channel_id': None}, None).deliver_as_bot('s1', 'hi')
        self.assertEqual(out['status'], 'no_channel')

    def test_no_session(self):
        out = self._runtime(None, None).deliver_as_bot('s1', 'hi')
        self.assertEqual((out['saved'], out['status']), (False, 'no_session'))


if __name__ == '__main__':
    unittest.main()
