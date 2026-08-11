"""Codex streams that fail must not look like successful empty answers.

An empty-but-successful result reaches the agent loop as a valid final answer
and gets rendered as "(No response)" — no retry, no fallback, no error in the
log. These cover the failure shapes the Responses API can deliver over SSE.
"""

import json
import os
import sys
from unittest.mock import patch

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from backend.provider.codex_client import CodexClient


class _FakeStream:
    def __init__(self, events, status_code=200):
        self.status_code = status_code
        self._events = events

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        for event in self._events:
            yield f"data: {json.dumps(event)}"

    def read(self):
        return b""


class _FakeClient:
    def __init__(self, stream):
        self._stream = stream
        self.payload = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, json=None, headers=None):
        self.payload = json
        return self._stream


def _send(events, **kwargs):
    """Run send_request against a canned SSE event list; returns (result, payload)."""
    client = CodexClient("token", "https://chatgpt.com/backend-api/codex")
    fake = _FakeClient(_FakeStream(events))
    with patch('backend.provider.codex_client.httpx.Client', return_value=fake):
        result = client.send_request(
            model=kwargs.pop('model', 'gpt-5-codex'),
            messages=kwargs.pop('messages', [{"role": "user", "content": "hi"}]),
            **kwargs,
        )
    return result, fake.payload


TEXT_EVENTS = [
    {"type": "response.created", "response": {"id": "resp_1", "model": "gpt-5-codex"}},
    {"type": "response.output_text.delta", "delta": "hello"},
    {"type": "response.completed", "response": {"id": "resp_1", "usage": {"input_tokens": 5, "output_tokens": 2}}},
]


def test_normal_stream_still_succeeds():
    result, _ = _send(TEXT_EVENTS)
    assert result["success"] is True
    assert result["response"]["choices"][0]["message"]["content"] == "hello"


def test_failed_response_is_an_error_not_empty_success():
    result, _ = _send([
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.failed", "response": {
            "id": "resp_1",
            "error": {"code": "server_error", "message": "backend blew up"}}},
    ])
    assert result["success"] is False
    assert result["error_type"] == "provider_error"
    assert "backend blew up" in result["error"]


def test_usage_limit_maps_to_rate_limit_error():
    result, _ = _send([
        {"type": "response.failed", "response": {
            "error": {"code": "usage_limit_reached", "message": "plan limit"}}},
    ])
    assert result["success"] is False
    assert result["error_type"] == "rate_limit_error"


def test_incomplete_on_max_output_tokens_is_an_error():
    result, _ = _send([
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.incomplete", "response": {
            "id": "resp_1", "incomplete_details": {"reason": "max_output_tokens"}}},
    ])
    assert result["success"] is False
    assert result["error_type"] == "llm_error"
    assert "max_output_tokens" in result["error"]


def test_bare_error_event_is_an_error():
    result, _ = _send([
        {"type": "error", "error": {"code": "rate_limit_exceeded", "message": "slow down"}},
    ])
    assert result["success"] is False
    assert result["error_type"] == "rate_limit_error"


def test_stream_ending_without_completion_is_an_error():
    result, _ = _send([
        {"type": "response.created", "response": {"id": "resp_1"}},
    ])
    assert result["success"] is False
    assert result["error_type"] == "provider_error"
    assert "without a completed response" in result["error"]


def test_partial_output_before_failure_is_kept():
    result, _ = _send([
        {"type": "response.output_text.delta", "delta": "partial answer"},
        {"type": "response.incomplete", "response": {
            "incomplete_details": {"reason": "max_output_tokens"}}},
    ])
    assert result["success"] is True
    assert result["response"]["choices"][0]["message"]["content"] == "partial answer"


def test_tool_call_without_text_is_not_treated_as_empty():
    result, _ = _send([
        {"type": "response.output_item.done", "item": {
            "type": "function_call", "call_id": "call_1",
            "name": "read_file", "arguments": '{"path": "a.txt"}'}},
        {"type": "response.completed", "response": {"id": "resp_1"}},
    ])
    assert result["success"] is True
    assert result["response"]["choices"][0]["message"]["tool_calls"][0]["id"] == "call_1"


# ── non-streaming path ───────────────────────────────────────────────


def _blocking(data, status_code=200):
    class _Resp:
        def __init__(self):
            self.status_code = status_code
            self.text = json.dumps(data)

        def json(self):
            return data

    client = CodexClient("token", "https://chatgpt.com/backend-api/codex")
    with patch('backend.provider.codex_client.httpx.post', return_value=_Resp()):
        return client.send_request(
            model='gpt-5-codex',
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
        )


def test_blocking_incomplete_without_output_is_an_error():
    result = _blocking({"id": "r", "status": "incomplete",
                        "incomplete_details": {"reason": "max_output_tokens"},
                        "output": []})
    assert result["success"] is False
    assert result["error_type"] == "llm_error"


def test_blocking_failed_without_output_is_an_error():
    result = _blocking({"id": "r", "status": "failed",
                        "error": {"code": "server_error", "message": "boom"},
                        "output": []})
    assert result["success"] is False
    assert result["error_type"] == "provider_error"
    assert "boom" in result["error"]


def test_blocking_completed_response_still_succeeds():
    result = _blocking({"id": "r", "status": "completed", "model": "gpt-5-codex",
                        "output": [{"type": "message", "content": [
                            {"type": "output_text", "text": "done"}]}]})
    assert result["success"] is True
    assert result["response"]["choices"][0]["message"]["content"] == "done"


# ── message conversion ───────────────────────────────────────────────


def test_assistant_text_is_kept_alongside_tool_calls():
    converted = CodexClient._convert_messages([
        {"role": "assistant", "content": "Let me check that file.",
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]},
    ])
    assert converted[0] == {"role": "assistant", "content": "Let me check that file."}
    assert converted[1]["type"] == "function_call"
    assert converted[1]["call_id"] == "call_1"


def test_empty_assistant_message_is_dropped():
    assert CodexClient._convert_messages([{"role": "assistant", "content": ""}]) == []
    assert CodexClient._convert_messages([{"role": "assistant", "content": None}]) == []


def test_tool_result_keeps_its_call_id():
    converted = CodexClient._convert_messages([
        {"role": "tool", "tool_call_id": "call_1", "content": "file body"},
    ])
    assert converted == [{"type": "function_call_output",
                          "call_id": "call_1", "output": "file body"}]


# ── output cap ───────────────────────────────────────────────────────


def test_max_output_tokens_not_sent_to_chatgpt_backend():
    _, payload = _send(TEXT_EVENTS, max_tokens=4096)
    assert "max_output_tokens" not in payload


def test_max_output_tokens_sent_to_standard_endpoint():
    client = CodexClient("token", "https://api.openai.com/v1")
    fake = _FakeClient(_FakeStream(TEXT_EVENTS))
    with patch('backend.provider.codex_client.httpx.Client', return_value=fake):
        client.send_request(model='gpt-5-codex',
                            messages=[{"role": "user", "content": "hi"}],
                            max_tokens=4096)
    assert fake.payload["max_output_tokens"] == 4096
