"""Request-payload options and reachability markers for LLMClient.

Covers the opt-in ``chat_template_kwargs`` escape hatch (used by the model ping
to ask self-hosted thinking models to skip chain-of-thought), the opt-in
``reasoning_effort`` passthrough for OpenAI-compatible providers, and the
``answered`` marker set when a reasoning block consumed the whole budget.
"""

from unittest.mock import patch

from backend.llm_client import LLMClient


def _client(**overrides):
    client = LLMClient.__new__(LLMClient)
    client.api_format = "openai"
    client.base_url = "http://llm.example/v1"
    client.model = "groot"
    client.provider = "llama.cpp"
    client._cached_model_name = "groot"
    client.max_tokens = 32
    client.temperature = None
    client.thinking = False
    client.thinking_budget = 0
    client.reasoning_effort = None
    client.timeout = 30
    setattr(client, "api_key", "")
    client.max_retries = 0
    client._allow_fallback = False
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


def _response(body):
    return type("Response", (), {
        "status_code": 200,
        "text": "",
        "headers": {"Content-Type": "application/json"},
        "json": lambda self: body,
    })()


def _openai_body(content="pong", finish_reason="stop", reasoning=""):
    message = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "id": "chatcmpl-test",
        "model": "groot",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    }


def test_chat_template_kwargs_are_merged_into_openai_payload():
    client = _client(chat_template_kwargs={"enable_thinking": False})
    with patch("backend.llm_client.requests.post", return_value=_response(_openai_body())) as post, \
         patch("backend.llm_client.log_api_call"):
        result = client.chat_completion(
            [{"role": "user", "content": "ping"}], max_tokens=32, enable_thinking=False)

    assert result["success"] is True, result
    assert post.call_args.kwargs["json"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_chat_template_kwargs_are_absent_unless_opted_in():
    client = _client()
    with patch("backend.llm_client.requests.post", return_value=_response(_openai_body())) as post, \
         patch("backend.llm_client.log_api_call"):
        result = client.chat_completion([{"role": "user", "content": "ping"}], max_tokens=32)

    assert result["success"] is True, result
    assert "chat_template_kwargs" not in post.call_args.kwargs["json"]


def test_chat_template_kwargs_never_reach_the_ollama_protocol():
    client = _client(api_format="ollama", adapter_key="ollama.chat.v1",
                     base_url="http://ollama.example:11434/v1",
                     chat_template_kwargs={"enable_thinking": False})
    body = {"message": {"role": "assistant", "content": "pong"},
            "done": True, "prompt_eval_count": 3, "eval_count": 1}
    with patch("backend.llm_client.requests.post", return_value=_response(body)) as post, \
         patch("backend.llm_client.log_api_call"):
        result = client.chat_completion([{"role": "user", "content": "ping"}], max_tokens=32)

    assert result["success"] is True, result
    assert "chat_template_kwargs" not in post.call_args.kwargs["json"]


def test_reasoning_only_completion_is_marked_answered_but_not_successful():
    client = _client()
    body = _openai_body(content="", finish_reason="length",
                        reasoning="Thinking Process: the user asked for pong")
    with patch("backend.llm_client.requests.post", return_value=_response(body)), \
         patch("backend.llm_client.log_api_call"):
        result = client.chat_completion(
            [{"role": "user", "content": "ping"}], max_tokens=32, enable_thinking=False)

    assert result["success"] is False
    assert result["error_type"] == "generation_timeout"
    assert result["answered"] is True


def test_reasoning_effort_is_forwarded_to_openai_payload():
    client = _client(reasoning_effort="max")
    with patch("backend.llm_client.requests.post", return_value=_response(_openai_body())) as post, \
         patch("backend.llm_client.log_api_call"):
        result = client.chat_completion([{"role": "user", "content": "ping"}], max_tokens=32)

    assert result["success"] is True, result
    assert post.call_args.kwargs["json"]["reasoning_effort"] == "max"


def test_reasoning_effort_is_absent_when_unset():
    client = _client()
    with patch("backend.llm_client.requests.post", return_value=_response(_openai_body())) as post, \
         patch("backend.llm_client.log_api_call"):
        result = client.chat_completion([{"role": "user", "content": "ping"}], max_tokens=32)

    assert result["success"] is True, result
    assert "reasoning_effort" not in post.call_args.kwargs["json"]


def test_blank_reasoning_effort_is_treated_as_unset():
    # Settings > Models sends "" for "Provider default"; it must not reach the
    # provider as an empty string.
    client = _client(reasoning_effort="")
    with patch("backend.llm_client.requests.post", return_value=_response(_openai_body())) as post, \
         patch("backend.llm_client.log_api_call"):
        result = client.chat_completion([{"role": "user", "content": "ping"}], max_tokens=32)

    assert result["success"] is True, result
    assert "reasoning_effort" not in post.call_args.kwargs["json"]


def test_reasoning_effort_never_reaches_the_anthropic_protocol():
    client = _client(api_format="anthropic",
                     base_url="http://anthropic.example/v1",
                     reasoning_effort="max",
                     _model_api_key_override=True)
    body = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "pong"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }
    with patch("backend.llm_client.requests.post", return_value=_response(body)) as post, \
         patch("backend.llm_client.log_api_call"):
        result = client.chat_completion([{"role": "user", "content": "ping"}], max_tokens=32)

    assert result["success"] is True, result
    assert post.call_args.args[0].endswith("/v1/messages")
    assert "reasoning_effort" not in post.call_args.kwargs["json"]


def test_reasoning_effort_never_reaches_the_ollama_protocol():
    client = _client(api_format="ollama",
                     adapter_key="ollama.chat.v1",
                     base_url="http://ollama.example:11434/api",
                     reasoning_effort="max")
    body = {"message": {"role": "assistant", "content": "pong"},
            "done": True, "prompt_eval_count": 3, "eval_count": 1}
    with patch("backend.llm_client.requests.post", return_value=_response(body)) as post, \
         patch("backend.llm_client.log_api_call"):
        result = client.chat_completion([{"role": "user", "content": "ping"}], max_tokens=32)

    assert result["success"] is True, result
    assert post.call_args.args[0].endswith("/api/chat")
    assert "reasoning_effort" not in post.call_args.kwargs["json"]


def _read_timeout_used(**overrides):
    client = _client(**overrides)
    with patch("backend.llm_client.requests.post", return_value=_response(_openai_body())) as post, \
         patch("backend.llm_client.log_api_call"):
        client.chat_completion([{"role": "user", "content": "ping"}])
    return post.call_args.kwargs["timeout"]


def test_read_timeout_defaults_when_model_has_none():
    assert _read_timeout_used(timeout=None) == (10, 900)


def test_explicit_model_timeout_wins():
    assert _read_timeout_used(timeout=30) == (10, 30)
