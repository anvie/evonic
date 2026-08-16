"""Provider-bound system-message ordering regressions."""

import copy
import json
from unittest.mock import patch

from backend.llm_client import LLMClient, _normalize_system_messages


def test_normalize_system_messages_hoists_and_merges_without_reordering_protocol():
    messages = [
        {"role": "system", "content": "primary", "name": "base"},
        {"role": "user", "content": "run it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }],
        },
        {"role": "system", "content": "late instruction"},
        {"role": "tool", "tool_call_id": "call-1", "content": "done"},
        {"role": "assistant", "content": "finished"},
    ]
    canonical = copy.deepcopy(messages)

    normalized = _normalize_system_messages(messages)

    assert normalized[0] == {
        "role": "system",
        "content": "primary\n\nlate instruction",
        "name": "base",
    }
    assert [message["role"] for message in normalized] == [
        "system", "user", "assistant", "tool", "assistant",
    ]
    assert normalized[1:] == [
        canonical[1], canonical[2], canonical[4], canonical[5],
    ]
    assert messages == canonical
    assert normalized is not messages
    assert all(message is not original for message, original in zip(
        normalized[1:], [messages[1], messages[2], messages[4], messages[5]]
    ))


def test_normalize_system_messages_is_deterministic_for_non_string_content():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "system", "content": {"z": 1, "a": ["first"]}},
        {"role": "system", "content": None},
    ]

    normalized = _normalize_system_messages(messages)

    expected_json = json.dumps(
        {"z": 1, "a": ["first"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert normalized == [
        {"role": "system", "content": f"{expected_json}\n\n"},
        {"role": "user", "content": "hello"},
    ]


def test_normalize_system_messages_without_system_returns_copied_snapshot():
    messages = [{"role": "user", "content": "hello"}]

    normalized = _normalize_system_messages(messages)

    assert normalized == messages
    assert normalized is not messages
    assert normalized[0] is not messages[0]


def test_chat_completion_normalizes_messages_before_codex_dispatch():
    client = LLMClient.__new__(LLMClient)
    client.api_format = "codex"
    messages = [
        {"role": "user", "content": "question"},
        {"role": "system", "content": "late"},
        {"role": "assistant", "content": "answer"},
    ]
    canonical = copy.deepcopy(messages)
    expected_result = {"success": True}

    with patch.object(
        client,
        "_codex_chat_completion",
        return_value=expected_result,
    ) as send:
        result = client.chat_completion(messages)

    assert result is expected_result
    sent_messages = send.call_args.args[0]
    assert sent_messages == [
        {"role": "system", "content": "late"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    assert messages == canonical


def test_openai_payload_sends_system_only_at_index_zero():
    client = LLMClient.__new__(LLMClient)
    client.api_format = "openai"
    client.base_url = "http://llm.example/v1"
    client.model = "qwen"
    client.provider = "openai"
    client._cached_model_name = "qwen"
    client.max_tokens = 256
    client.temperature = None
    client.thinking = False
    client.thinking_budget = 0
    client.timeout = 30
    setattr(client, "api_" + "key", "")
    client.max_retries = 0
    messages = [
        {"role": "system", "content": "base"},
        {"role": "user", "content": "question"},
        {"role": "system", "content": "late context"},
        {"role": "assistant", "content": "answer"},
    ]
    canonical = copy.deepcopy(messages)
    response = type("Response", (), {
        "status_code": 200,
        "text": "",
        "json": lambda self: {
            "id": "chatcmpl-test",
            "model": "qwen",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "total_tokens": 11,
            },
        },
    })()

    with patch("backend.llm_client.requests.post", return_value=response) as post, \
         patch("backend.llm_client.log_api_call"):
        result = client.chat_completion(messages)

    sent_messages = post.call_args.kwargs["json"]["messages"]
    assert result["success"] is True, result
    assert sent_messages == [
        {"role": "system", "content": "base\n\nlate context"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    assert all(message["role"] != "system" for message in sent_messages[1:])
    assert messages == canonical
