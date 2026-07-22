"""Tests for unified chat SSE buffer-replay on connect (routes.realtime).

A client connecting at/after the POST that starts a turn must still receive the
turn's opening events (turn_begin, early thinking, first tool call). The unified
_producer_chat replays the in-progress session buffer after subscribing — mirror
of the legacy /chat/stream behavior. Without it the UI shows only a spinner until
a manual refresh.
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.realtime import _producer_chat, BoundedRing, CircuitBreaker
from backend.event_stream import event_stream


def _run_producer(session_id, after_seq=0, settle=0.2):
    """Run _producer_chat briefly and return the (sse_name, payload) items it
    pushed into the ring during the replay phase."""
    ring = BoundedRing('chat', 256, 'drop_oldest')
    stop = threading.Event()
    th = threading.Thread(
        target=_producer_chat,
        args=(ring, CircuitBreaker('chat'), stop, session_id, after_seq),
        daemon=True,
    )
    th.start()
    time.sleep(settle)  # allow subscribe + replay
    stop.set()
    th.join(timeout=2)
    return [item for (_seq, item) in ring.get_all()]


class TestChatBufferReplay(unittest.TestCase):

    def test_replays_in_progress_turn(self):
        sid = 'replaytest-inprogress'
        event_stream.emit('turn_begin', {'session_id': sid, 'ts': 1})
        event_stream.emit('llm_thinking', {'session_id': sid, 'thinking': 'hmm'})
        event_stream.emit('tool_call_started',
                          {'session_id': sid, 'tool_name': 'bash', 'tool_args': {}})

        items = _run_producer(sid)
        names = [n for (n, _p) in items]
        self.assertEqual(names, ['turn_begin', 'thinking', 'tool_call_started'])

        # Every replayed event carries the contiguous per-session chat seq.
        seqs = [p.get('seq') for (_n, p) in items]
        self.assertTrue(all(s is not None for s in seqs))
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), 3)

    def test_strips_completed_turn_on_fresh_connect(self):
        sid = 'replaytest-boundary'
        event_stream.emit('turn_begin', {'session_id': sid, 'ts': 1})
        event_stream.emit('llm_thinking', {'session_id': sid, 'thinking': 'old'})
        event_stream.emit('turn_complete', {'session_id': sid, 'thinking_duration': 1.0})
        # A new in-progress turn after the boundary — only this should replay.
        event_stream.emit('turn_begin', {'session_id': sid, 'ts': 2})
        event_stream.emit('llm_thinking', {'session_id': sid, 'thinking': 'new'})

        items = _run_producer(sid, after_seq=0)
        names = [n for (n, _p) in items]
        self.assertEqual(names, ['turn_begin', 'thinking'])
        # The replayed thinking is from the NEW turn, not the stripped one.
        thinking = [p for (n, p) in items if n == 'thinking'][0]
        self.assertEqual(thinking.get('content'), 'new')

    def test_no_buffer_is_noop(self):
        items = _run_producer('replaytest-empty')
        self.assertEqual(items, [])

    def test_message_received_forwarded_with_seq(self):
        sid = 'replaytest-message-received'
        event_stream.emit('message_received', {
            'session_id': sid,
            'message': '[ESCALATION/Agent B] Please choose.',
            'metadata': {'escalated_from_agent_session': True},
        })

        items = _run_producer(sid)
        received = [p for (name, p) in items if name == 'message_received']
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['message'], '[ESCALATION/Agent B] Please choose.')
        self.assertTrue(received[0]['metadata']['escalated_from_agent_session'])
        self.assertIsNotNone(received[0]['seq'])

    def test_state_changed_forwarded_with_seq(self):
        """'evonic:agent-state-changed' is a chat-forwarded event: it gets a
        contiguous chat seq (so the client's gap detector can recover one
        that loses the thread-pool race against turn_complete) and reaches
        the stream as 'state_changed'."""
        sid = 'replaytest-statechanged'
        event_stream.emit('turn_begin', {'session_id': sid, 'ts': 1})
        event_stream.emit('evonic:agent-state-changed',
                          {'agent_id': 'a1', 'session_id': sid})
        items = _run_producer(sid)
        names = [n for (n, _p) in items]
        self.assertIn('state_changed', names)
        payload = [p for (n, p) in items if n == 'state_changed'][0]
        self.assertIsNotNone(payload.get('seq'))
        self.assertEqual(payload.get('agent_id'), 'a1')


if __name__ == '__main__':
    unittest.main()
