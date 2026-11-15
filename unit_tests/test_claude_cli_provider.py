"""Claude subscription access through the official `claude -p` CLI."""

import json
import subprocess
from unittest.mock import patch

from backend.provider import claude_cli

TOOLS = [{"type": "function", "function": {
    "name": "get_weather", "description": "Weather",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]


def _proc(payload, returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=json.dumps(payload), stderr=stderr)


def _run(payload, messages=None, **kwargs):
    messages = messages or [{"role": "system", "content": "Be brief."},
                            {"role": "user", "content": "hi"}]
    with patch.object(claude_cli, "claude_binary", return_value="/usr/bin/claude"), \
         patch("backend.provider.claude_cli.subprocess.run", return_value=_proc(payload)) as run:
        result = claude_cli.send_request("claude-sonnet-5-5", messages, **kwargs)
    return result, run


def test_plain_completion_disables_builtin_tools_and_maps_usage():
    result, run = _run({"result": "Hello!", "usage": {
        "input_tokens": 2, "cache_creation_input_tokens": 600,
        "cache_read_input_tokens": 10, "output_tokens": 5}})
    cmd = run.call_args.args[0]
    assert result["success"]
    assert result["response"]["choices"][0]["message"]["content"] == "Hello!"
    assert result["response"]["usage"] == {
        "prompt_tokens": 612, "completion_tokens": 5, "total_tokens": 617,
        "prompt_tokens_details": {"cached_tokens": 10}}
    assert cmd[cmd.index("--tools") + 1] == ""
    for flag in ("--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == "Be brief."
    assert "--json-schema" not in cmd
    assert "[user]\nhi" in run.call_args.kwargs["input"]


def test_tool_calls_are_emulated_and_filtered_to_known_tools():
    result, run = _run({"result": "", "structured_output": {
        "text": "Checking.", "tool_calls": [
            {"name": "get_weather", "arguments": {"city": "Tokyo"}},
            {"name": "rm_rf", "arguments": {}}]},
        "usage": {}}, tools=TOOLS)
    message = result["response"]["choices"][0]["message"]
    assert "--json-schema" in run.call_args.args[0]
    assert [c["function"]["name"] for c in message["tool_calls"]] == ["get_weather"]
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"city": "Tokyo"}
    assert result["response"]["choices"][0]["finish_reason"] == "tool_calls"


def test_tool_history_is_flattened_into_the_transcript():
    messages = [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "get_weather", "arguments": "{\"city\":\"Tokyo\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "sunny"},
    ]
    _, run = _run({"result": "ok", "usage": {}}, messages=messages)
    sent = run.call_args.kwargs["input"]
    assert "tool_call id=c1 name=get_weather" in sent
    assert "[tool_result id=c1]\nsunny" in sent


def test_errors_are_classified():
    for detail, expected in (("Not logged in · Please run /login", "auth_error"),
                             ("Claude usage limit reached", "rate_limit_error")):
        with patch.object(claude_cli, "claude_binary", return_value="/usr/bin/claude"), \
             patch("backend.provider.claude_cli.subprocess.run",
                   return_value=_proc({"is_error": True, "result": detail}, returncode=1)):
            result = claude_cli.send_request("m", [{"role": "user", "content": "x"}])
        assert not result["success"] and result["error_type"] == expected


def test_missing_binary_and_timeout():
    with patch.object(claude_cli, "claude_binary", return_value=None):
        assert claude_cli.send_request("m", [{"role": "user", "content": "x"}])["error_type"] == "connection_error"
    with patch.object(claude_cli, "claude_binary", return_value="/usr/bin/claude"), \
         patch("backend.provider.claude_cli.subprocess.run",
               side_effect=subprocess.TimeoutExpired("claude", 1)):
        assert claude_cli.send_request("m", [{"role": "user", "content": "x"}])["error_type"] == "timeout_error"


def test_llm_client_routes_claude_cli_format():
    from backend.llm_client import LLMClient

    client = LLMClient(model_config={"model_name": "claude-sonnet-5-5", "api_format": "claude_cli",
                                     "provider": "claude-code", "base_url": ""})
    fake = {"success": True, "duration_ms": 5, "response": {
        "choices": [{"message": {"role": "assistant", "content": "yo"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4,
                  "prompt_tokens_details": {"cached_tokens": 0}}}}
    # Resolving the model against its seeded "claude-code" provider must not
    # coerce the direct CLI format into an HTTP adapter (which would send the
    # request to an empty OpenAI URL).
    assert client.api_format == "claude_cli"
    with patch("backend.provider.claude_cli.send_request", return_value=fake) as send:
        result = client.chat_completion([{"role": "user", "content": "hi"}])
    assert send.called
    assert result["success"] and client.extract_content(result) == "yo"


def test_effort_follows_thinking_budget():
    assert claude_cli.resolve_effort(False, 50_000) == "low"
    assert claude_cli.resolve_effort(True, 0) == "medium"
    assert claude_cli.resolve_effort(True, 4_000) == "medium"
    assert claude_cli.resolve_effort(True, 16_000) == "high"
    assert claude_cli.resolve_effort(True, 100_000) == "max"
    _, run = _run({"result": "ok", "usage": {}}, thinking=True, thinking_budget=16_000)
    cmd = run.call_args.args[0]
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert run.call_args.kwargs["env"]["DISABLE_AUTO_COMPACT"] == "1"


def test_tool_instructions_forbid_native_tool_calls():
    text = claude_cli._tool_instructions(
        [{"type": "function", "function": {"name": "bash", "description": "", "parameters": {}}}], None)
    assert "never invoke them as native tool calls" in text
    assert "tool_calls" in text


def test_images_are_sent_as_stream_json_blocks():
    url = "data:image/png;base64,QUJD"
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": url}}]}]
    event = json.dumps({"type": "result", "result": "a dashboard", "usage": {}})
    proc = subprocess.CompletedProcess([], 0, stdout='{"type":"system"}\n' + event + "\n", stderr="")
    with patch.object(claude_cli, "claude_binary", return_value="/usr/bin/claude"), \
         patch("backend.provider.claude_cli.subprocess.run", return_value=proc) as run:
        result = claude_cli.send_request("claude-sonnet-5-5", messages)
    cmd = run.call_args.args[0]
    assert result["response"]["choices"][0]["message"]["content"] == "a dashboard"
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    sent = json.loads(run.call_args.kwargs["input"])
    blocks = sent["message"]["content"]
    image = next(b for b in blocks if b["type"] == "image")
    assert image["source"] == {"type": "base64", "media_type": "image/png", "data": "QUJD"}
    assert "[image 1, shown below]" in blocks[blocks.index(image) - 1]["text"]


def _picture(n):
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{n}"}}


def test_each_image_sits_where_it_was_mentioned_and_old_ones_are_dropped():
    count = claude_cli.MAX_IMAGES + 2
    messages = []
    for n in range(count):
        messages.append({"role": "user", "content": [{"type": "text", "text": f"pic {n}"}, _picture(f"AA{n}")]})
        messages.append({"role": "assistant", "content": f"seen {n}"})
    blocks = claude_cli._stream_content(
        claude_cli._format_transcript(messages, (images := [])), images)

    sent = [b["source"]["data"] for b in blocks if b["type"] == "image"]
    assert sent == [f"AA{n}" for n in range(2, count)]
    text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
    assert "[image 1: an older picture, no longer attached]" in text
    assert f"[image {count}, shown below]" in text
    # Every picture follows its own label, directly.
    for i, block in enumerate(blocks):
        if block["type"] == "image":
            assert blocks[i - 1]["text"].rstrip().endswith("shown below]")
