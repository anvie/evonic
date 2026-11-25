"""Streaming chat completions: SSE deltas are reassembled into the same
response shape a non-streaming call returns, and thinking deltas reach the
caller live."""

import json
from unittest.mock import patch

import pytest
import requests

from backend.llm_client import LLMClient, _consume_openai_stream


class FakeStreamResponse:
    def __init__(self, frames, status=200, content_type='text/event-stream', body_text=''):
        self._frames = frames
        self.status_code = status
        self.headers = {'Content-Type': content_type}
        self.text = body_text
        self.closed = False

    def iter_lines(self):
        for frame in self._frames:
            yield frame.encode() if isinstance(frame, str) else frame

    def json(self):
        return json.loads(self.text)

    def close(self):
        self.closed = True


def sse(obj):
    return 'data: ' + json.dumps(obj)


def chunk(delta=None, finish=None, **extra):
    c = {'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}]}
    c.update(extra)
    return sse(c)


def test_assembles_reasoning_content_and_usage():
    seen = []
    resp = FakeStreamResponse([
        chunk({'role': 'assistant', 'reasoning_content': 'Let me '}),
        chunk({'reasoning_content': 'think.'}),
        chunk({'content': 'Hel'}),
        chunk({'content': 'lo'}, finish='stop'),
        sse({'choices': [], 'usage': {'prompt_tokens': 3, 'completion_tokens': 5,
                                      'total_tokens': 8}}),
        'data: [DONE]',
    ])
    out = _consume_openai_stream(resp, lambda k, t: seen.append((k, t)))
    msg = out['choices'][0]['message']
    assert msg['content'] == 'Hello'
    assert msg['reasoning_content'] == 'Let me think.'
    assert out['choices'][0]['finish_reason'] == 'stop'
    assert out['usage']['total_tokens'] == 8
    assert seen == [('thinking', 'Let me '), ('thinking', 'think.')]


def test_reasoning_field_alias_is_streamed_too():
    seen = []
    resp = FakeStreamResponse([chunk({'reasoning': 'abc'}), chunk(finish='stop'), 'data: [DONE]'])
    out = _consume_openai_stream(resp, lambda k, t: seen.append(t))
    assert out['choices'][0]['message']['reasoning_content'] == 'abc'
    assert seen == ['abc']


def test_tool_call_deltas_are_merged_per_index():
    resp = FakeStreamResponse([
        chunk({'tool_calls': [{'index': 0, 'id': 'c1', 'type': 'function',
                               'function': {'name': 'read_', 'arguments': ''}}]}),
        chunk({'tool_calls': [{'index': 0, 'function': {'name': 'file', 'arguments': '{"pa'}}]}),
        chunk({'tool_calls': [{'index': 0, 'function': {'arguments': 'th":"a"}'}}]}),
        chunk({'tool_calls': [{'index': 1, 'id': 'c2', 'function': {'name': 'calc', 'arguments': '{}'}}]},
              finish='tool_calls'),
        'data: [DONE]',
    ])
    out = _consume_openai_stream(resp)
    calls = out['choices'][0]['message']['tool_calls']
    assert [c['id'] for c in calls] == ['c1', 'c2']
    assert calls[0]['function'] == {'name': 'read_file', 'arguments': '{"path":"a"}'}
    assert out['choices'][0]['finish_reason'] == 'tool_calls'


def test_error_frame_returns_error_body():
    resp = FakeStreamResponse([chunk({'content': 'x'}), sse({'error': {'message': 'overloaded', 'code': 529}})])
    assert _consume_openai_stream(resp) == {'error': {'message': 'overloaded', 'code': 529}}


def test_dropped_stream_raises_connection_error():
    resp = FakeStreamResponse([chunk({'content': 'partial'})])
    with pytest.raises(requests.exceptions.ConnectionError):
        _consume_openai_stream(resp)


def test_ignores_comments_blank_lines_and_garbage():
    resp = FakeStreamResponse([': keep-alive', '', 'data: {not json', 'event: ping',
                               chunk({'content': 'ok'}, finish='stop'), 'data: [DONE]'])
    assert _consume_openai_stream(resp)['choices'][0]['message']['content'] == 'ok'


# ── through LLMClient ──────────────────────────────────────────────────────

CFG = {'provider': 'p', 'base_url': 'https://x.example/v1', 'api_key': 'k',
       'model_name': 'm', 'timeout': 5, 'thinking': False, 'api_format': 'openai'}


@pytest.fixture(autouse=True)
def _no_fallback():
    with patch.object(LLMClient, '_get_fallback_client', return_value=None):
        yield


def make_client():
    return LLMClient(model_config=dict(CFG))


def test_chat_completion_streams_and_returns_normal_shape():
    seen = []
    resp = FakeStreamResponse([
        chunk({'reasoning_content': 'hmm'}), chunk({'content': 'done'}, finish='stop'),
        sse({'choices': [], 'usage': {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3}}),
        'data: [DONE]'])
    with patch('backend.llm_client.requests.post', return_value=resp) as post:
        out = make_client().chat_completion([{'role': 'user', 'content': 'hi'}],
                                            stream_callback=lambda k, t: seen.append((k, t)))
    sent = post.call_args.kwargs
    assert sent['json']['stream'] is True
    assert sent['json']['stream_options'] == {'include_usage': True}
    assert sent['stream'] is True
    assert out['success'] is True
    assert out['response']['choices'][0]['message']['content'] == 'done'
    assert out['response']['choices'][0]['message']['reasoning_content'] == 'hmm'
    assert seen == [('thinking', 'hmm')]
    assert out['duration_ms'] >= 0


def test_no_callback_keeps_non_streaming_request():
    body = json.dumps({'choices': [{'message': {'content': 'hi'}, 'finish_reason': 'stop'}], 'usage': {}})
    resp = FakeStreamResponse([], content_type='application/json', body_text=body)
    with patch('backend.llm_client.requests.post', return_value=resp) as post:
        out = make_client().chat_completion([{'role': 'user', 'content': 'hi'}])
    assert post.call_args.kwargs['json']['stream'] is False
    assert 'stream_options' not in post.call_args.kwargs['json']
    assert out['success'] is True


def test_provider_that_ignores_stream_and_returns_json_still_works():
    body = json.dumps({'choices': [{'message': {'content': 'plain'}, 'finish_reason': 'stop'}], 'usage': {}})
    resp = FakeStreamResponse([], content_type='application/json', body_text=body)
    with patch('backend.llm_client.requests.post', return_value=resp):
        out = make_client().chat_completion([{'role': 'user', 'content': 'hi'}],
                                            stream_callback=lambda k, t: None)
    assert out['response']['choices'][0]['message']['content'] == 'plain'


def test_400_on_stream_options_falls_back_to_non_streaming():
    rejected = FakeStreamResponse([], status=400, body_text='unknown field stream_options')
    body = json.dumps({'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}], 'usage': {}})
    ok = FakeStreamResponse([], content_type='application/json', body_text=body)
    with patch('backend.llm_client.requests.post', side_effect=[rejected, ok]) as post:
        out = make_client().chat_completion([{'role': 'user', 'content': 'hi'}],
                                            stream_callback=lambda k, t: None)
    assert post.call_count == 2
    second = post.call_args_list[1].kwargs['json']
    assert second['stream'] is False and 'stream_options' not in second
    assert rejected.closed is True
    assert out['success'] is True


def test_retry_after_dropped_stream_sends_reset():
    events = []
    dropped = FakeStreamResponse([chunk({'reasoning_content': 'part'})])
    good = FakeStreamResponse([chunk({'content': 'fine'}, finish='stop'), 'data: [DONE]'])
    with patch('backend.llm_client.requests.post', side_effect=[dropped, good]), \
         patch('backend.llm_client.time.sleep'):
        out = make_client().chat_completion([{'role': 'user', 'content': 'hi'}],
                                            stream_callback=lambda k, t: events.append(k))
    assert out['success'] is True
    assert events == ['thinking', 'reset']


# ── real sockets: deltas must arrive as they are sent, not when the stream ends ──

def _serve(chunked):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked') if chunked else self.send_header('Connection', 'close')
            self.end_headers()
            frames = [chunk({'reasoning_content': f't{i} '}) + '\n\n' for i in range(4)]
            frames += [chunk({'content': 'ok'}, finish='stop') + '\n\n', 'data: [DONE]\n\n']
            for frame in frames:
                data = frame.encode()
                self.wfile.write(f'{len(data):x}\r\n'.encode() + data + b'\r\n' if chunked else data)
                self.wfile.flush()
                time.sleep(0.15)
            if chunked:
                self.wfile.write(b'0\r\n\r\n')
            self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize('chunked', [True, False])
def test_deltas_arrive_progressively_over_a_real_socket(chunked):
    import time
    server = _serve(chunked)
    try:
        stamps = []
        start = time.time()
        resp = requests.post(f'http://127.0.0.1:{server.server_address[1]}/', json={},
                             stream=True, timeout=(5, 10))
        out = _consume_openai_stream(resp, lambda kind, text: stamps.append(time.time() - start))
    finally:
        server.shutdown()
    assert out['choices'][0]['message']['reasoning_content'] == 't0 t1 t2 t3 '
    # four deltas sent ~0.15s apart: the first must land long before the last
    assert len(stamps) == 4
    assert stamps[-1] - stamps[0] > 0.3
    assert stamps[0] < 0.3


def test_reasoning_only_stream_is_cut_at_the_limit_as_length_with_no_content():
    # 3 chars ~ 1 token: 10 frames x 30 chars = ~100 tokens of reasoning, limit 30
    resp = FakeStreamResponse(
        [chunk({'reasoning_content': 'x' * 30}) for _ in range(10)] +
        [chunk({'content': 'never reached'}, finish='stop'), 'data: [DONE]'])
    out = _consume_openai_stream(resp, reasoning_only_abort_tokens=30)
    choice = out['choices'][0]
    assert choice['finish_reason'] == 'length'
    assert choice['message']['content'] == ''
    assert len(choice['message']['reasoning_content']) < 300   # stopped early
    assert resp.closed


def test_reasoning_limit_does_not_cut_a_stream_that_already_answers():
    resp = FakeStreamResponse(
        [chunk({'content': 'Hi'})] +
        [chunk({'reasoning_content': 'x' * 30}) for _ in range(10)] +
        [chunk({}, finish='stop'), 'data: [DONE]'])
    out = _consume_openai_stream(resp, reasoning_only_abort_tokens=30)
    assert out['choices'][0]['finish_reason'] == 'stop'
    assert not resp.closed


def test_reasoning_limit_zero_disables_the_guard():
    resp = FakeStreamResponse(
        [chunk({'reasoning_content': 'x' * 30}) for _ in range(10)] +
        [chunk({'content': 'ok'}, finish='stop'), 'data: [DONE]'])
    out = _consume_openai_stream(resp, reasoning_only_abort_tokens=0)
    assert out['choices'][0]['message']['content'] == 'ok'


def test_effective_output_tokens_caps_thinking_doubling_but_keeps_small_budgets():
    from backend.llm_client import effective_output_tokens as eff
    assert eff(4096, True, True, 0) == 8192            # small budget: still doubled
    assert eff(16384, True, True, 0) == 32768
    assert eff(64000, True, True, 0) == 64000          # large budget: never doubled (was 128000)
    assert eff(64000, True, True, 8192) == 64000       # explicit thinking_budget: untouched
    assert eff(4096, True, False, 0) == 4096           # thinking disabled for this call
    assert eff(4096, False, True, 0) == 4096
    assert eff(None, True, True, 0) is None
