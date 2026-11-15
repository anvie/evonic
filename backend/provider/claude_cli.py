"""Claude subscription access through the official ``claude -p`` CLI.

Evonic never reads, copies or refreshes Claude OAuth tokens. The user logs in
with ``claude auth login`` and Evonic only spawns the CLI in print mode, so the
Claude Code client itself talks to Anthropic. The CLI runs here as a plain LLM:
its built-in tools, settings, MCP servers and session persistence are disabled,
and Evonic's own tools are emulated through ``--json-schema`` so the agent loop,
workplaces and safety checks in Evonic stay in charge of every action.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

DEFAULT_TIMEOUT = 300
STATUS_TTL = 30
# Above this size the system prompt goes through stdin instead of argv
# (single argv strings are capped at ~128 KB on Linux).
MAX_ARGV_SYSTEM_PROMPT = 100_000

KNOWN_MODELS = [
    {"id": "claude-fable-5-1", "name": "Claude Fable 5.1"},
    {"id": "claude-opus-5-5", "name": "Claude Opus 5.5"},
    {"id": "claude-sonnet-5-5", "name": "Claude Sonnet 5.5"},
    {"id": "claude-haiku-5-5", "name": "Claude Haiku 5.5"},
]

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["name", "arguments"],
            },
        },
    },
    "required": ["text"],
}

# Only the newest pictures are sent: older ones are long dealt with, and every
# image costs input tokens on each turn of the agent loop.
MAX_IMAGES = 4

# Same child-env policy as pi-claude-bridge: no claude.ai cloud MCP discovery,
# and Evonic owns context management so the CLI must not auto-compact.
CHILD_ENV = {
    "ENABLE_CLAUDEAI_MCP_SERVERS": "0",
    "DISABLE_AUTO_COMPACT": "1",
}

# Thinking-budget (tokens) upper bounds -> Claude Code ``--effort`` level.
_EFFORT_STEPS = ((2_048, "low"), (8_192, "medium"), (24_576, "high"), (65_536, "xhigh"))
_VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")

_status_lock = threading.Lock()
_status_cache: Dict[str, Any] = {"at": 0.0, "value": None}
_workdir: Optional[str] = None


def claude_binary() -> Optional[str]:
    return shutil.which("claude")


def _neutral_cwd() -> str:
    """Empty directory so no project CLAUDE.md or settings leak into prompts."""
    global _workdir
    if not _workdir or not os.path.isdir(_workdir):
        _workdir = tempfile.mkdtemp(prefix="evonic-claude-cli-")
    return _workdir


def get_status(force: bool = False) -> Dict[str, Any]:
    """Return ``{installed, logged_in, version, auth_method, error}`` (cached)."""
    with _status_lock:
        now = time.time()
        cached = _status_cache["value"]
        if cached is not None and not force and now - _status_cache["at"] < STATUS_TTL:
            return cached

    binary = claude_binary()
    status: Dict[str, Any] = {
        "installed": bool(binary), "logged_in": False, "version": "",
        "auth_method": "", "error": "",
    }
    if not binary:
        status["error"] = "Claude Code CLI not found. Install it, then run `claude auth login`."
    else:
        try:
            version = subprocess.run(
                [binary, "--version"], capture_output=True, text=True, timeout=10,
                stdin=subprocess.DEVNULL, cwd=_neutral_cwd())
            status["version"] = version.stdout.strip().split(" ")[0]
            auth = subprocess.run(
                [binary, "auth", "status"], capture_output=True, text=True, timeout=15,
                stdin=subprocess.DEVNULL, cwd=_neutral_cwd())
            info = json.loads(auth.stdout or "{}")
            status["logged_in"] = bool(info.get("loggedIn"))
            status["auth_method"] = info.get("authMethod") or ""
            if not status["logged_in"]:
                status["error"] = "Not logged in. Run `claude auth login` in a terminal."
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            status["error"] = f"Could not query Claude Code CLI: {exc}"

    with _status_lock:
        _status_cache.update(at=time.time(), value=status)
    return status


def resolve_effort(thinking: bool, budget: int = 0) -> str:
    """Map Evonic's thinking flag/budget onto a ``--effort`` level.

    Thinking off -> ``low``. Thinking on without a budget -> ``medium``; with a
    budget the closest level is chosen. An explicit level name is passed through.
    """
    if isinstance(budget, str) and budget.lower() in _VALID_EFFORTS:
        return budget.lower()
    if not thinking:
        return "low"
    try:
        budget = int(budget or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget <= 0:
        return "medium"
    for limit, level in _EFFORT_STEPS:
        if budget <= limit:
            return level
    return "max"


def _content_text(content: Any, images: Optional[List[Dict[str, Any]]] = None) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append(part.get("text", ""))
                elif part.get("type") in ("image_url", "image"):
                    block = _image_block(part)
                    if block is not None and images is not None:
                        images.append(block)
                        parts.append(f"[image {len(images)} attached]")
                    else:
                        parts.append("[image omitted: could not be read as an inline image]")
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(p for p in parts if p)
    return json.dumps(content, ensure_ascii=False)


_DATA_URL = re.compile(r"^data:(image/[\w.+-]+);base64,(.+)$", re.DOTALL)


def _image_block(part: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """OpenAI ``image_url`` (data URL) / Anthropic ``image`` part -> Claude image block."""
    if part.get("type") == "image" and isinstance(part.get("source"), dict):
        return {"type": "image", "source": part["source"]}
    url = part.get("image_url")
    url = url.get("url", "") if isinstance(url, dict) else (url or "")
    match = _DATA_URL.match(url)
    if not match:
        return None
    return {"type": "image", "source": {
        "type": "base64", "media_type": match.group(1), "data": match.group(2)}}


_IMAGE_MARKER = re.compile(r"\[image (\d+) attached\]")


def _stream_content(transcript: str, images: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Content blocks with each image right where the transcript mentions it.

    Putting every picture in front of the text left the model matching "[image 3
    attached]" against a pile of images, and it sometimes claimed it saw none.
    """
    first_sent = len(images) - MAX_IMAGES
    blocks: List[Dict[str, Any]] = []
    pos = 0

    def add_text(text: str) -> None:
        if text:
            blocks.append({"type": "text", "text": text})

    for match in _IMAGE_MARKER.finditer(transcript):
        number = int(match.group(1))
        add_text(transcript[pos:match.start()])
        pos = match.end()
        if number - 1 < first_sent:
            add_text(f"[image {number}: an older picture, no longer attached]")
        else:
            add_text(f"[image {number}, shown below]")
            blocks.append(images[number - 1])
    add_text(transcript[pos:])
    return blocks


def _format_transcript(messages: List[Dict[str, Any]],
                       images: Optional[List[Dict[str, Any]]] = None) -> str:
    """Flatten OpenAI-style messages (incl. tool calls/results) into a transcript."""
    blocks = []
    for message in messages:
        role = message.get("role")
        text = _content_text(message.get("content"), images)
        if role == "user":
            blocks.append(f"[user]\n{text}")
        elif role == "assistant":
            lines = [f"[assistant]\n{text}"] if text else []
            for call in message.get("tool_calls") or []:
                fn = call.get("function", {})
                lines.append(
                    f"[assistant tool_call id={call.get('id', '')} name={fn.get('name', '')}]\n"
                    f"{fn.get('arguments', '{}')}")
            if lines:
                blocks.append("\n".join(lines))
        elif role == "tool":
            blocks.append(f"[tool_result id={message.get('tool_call_id', '')}]\n{text}")
    return "\n\n".join(blocks)


def _tool_instructions(tools: List[Dict[str, Any]], tool_choice: Optional[str]) -> str:
    specs = []
    for tool in tools:
        fn = tool.get("function", {})
        specs.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        })
    rule = (f"You MUST call the tool `{tool_choice}` in this reply."
            if tool_choice else
            "Call tools only when they are needed; otherwise leave tool_calls empty.")
    return (
        "\n\n# Tools\n"
        "The tools listed below are NOT native function-calling tools in this session; "
        "never invoke them as native tool calls (that fails with 'No such tool "
        "available'). Instead you call them by writing them into your final structured "
        "reply, a JSON object with `text` (what you say to the user, may be empty when "
        "calling tools) and `tool_calls` (list of {name, arguments}). Evonic executes "
        "every entry in `tool_calls` and the conversation continues with [tool_result] "
        "entries. `arguments` must follow the tool's JSON schema. You have no other "
        f"tools. {rule}\n\n{json.dumps(specs, ensure_ascii=False)}")


def _final_result(stdout: str) -> Dict[str, Any]:
    """Pick the ``result`` event out of ``--output-format stream-json`` output."""
    for line in reversed((stdout or "").splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    raise json.JSONDecodeError("no result event", stdout or "", 0)


def _classify_error(text: str) -> str:
    low = (text or "").lower()
    if any(s in low for s in ("not logged in", "login", "authenticat", "401", "oauth token")):
        return "auth_error"
    if any(s in low for s in ("rate limit", "usage limit", "429", "limit reached")):
        return "rate_limit_error"
    if any(s in low for s in ("prompt is too long", "context")):
        return "context_length_error"
    if any(s in low for s in ("overloaded", "529", "500", "502", "503")):
        return "provider_error"
    return "llm_error"


def _fail(error_type: str, detail: str, duration_ms: int) -> Dict[str, Any]:
    return {"success": False, "error_type": error_type, "error": detail,
            "duration_ms": duration_ms}


def send_request(
    model: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[str] = None,
    thinking: bool = False,
    thinking_budget: int = 0,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """Run one completion. Returns ``{success, response}`` in OpenAI chat shape."""
    start = time.time()
    binary = claude_binary()
    if not binary:
        return _fail("connection_error", get_status()["error"], 0)

    system_parts = [_content_text(m.get("content")) for m in messages if m.get("role") == "system"]
    system_prompt = "\n\n".join(p for p in system_parts if p) or "You are a helpful assistant."
    if tools:
        system_prompt += _tool_instructions(tools, tool_choice)
    images: List[Dict[str, Any]] = []
    transcript = _format_transcript([m for m in messages if m.get("role") != "system"], images)

    cmd = [
        binary, "-p", "--model", model, "--output-format", "json",
        "--tools", "", "--no-session-persistence", "--disable-slash-commands",
        "--setting-sources", "", "--strict-mcp-config",
    ]
    cmd += ["--effort", resolve_effort(thinking, thinking_budget)]
    if tools:
        cmd += ["--json-schema", json.dumps(_OUTPUT_SCHEMA)]

    stdin_text = transcript or "[user]\n(empty)"
    if len(system_prompt.encode("utf-8")) <= MAX_ARGV_SYSTEM_PROMPT:
        cmd += ["--system-prompt", system_prompt]
    else:
        cmd += ["--system-prompt", "Follow the instructions in the <system> block of the input."]
        stdin_text = f"<system>\n{system_prompt}\n</system>\n\n{stdin_text}"

    if images:
        # Plain-text stdin cannot carry images; stream-json input can.
        cmd += ["--input-format", "stream-json", "--verbose"]
        cmd[cmd.index("--output-format") + 1] = "stream-json"
        stdin_text = json.dumps({"type": "user", "message": {
            "role": "user", "content": _stream_content(stdin_text, images)}}) + "\n"

    try:
        proc = subprocess.run(
            cmd, input=stdin_text, capture_output=True, text=True,
            timeout=timeout or DEFAULT_TIMEOUT, cwd=_neutral_cwd(),
            env={**os.environ, **CHILD_ENV})
    except subprocess.TimeoutExpired:
        return _fail("timeout_error", "claude -p timed out", int((time.time() - start) * 1000))
    except OSError as exc:
        return _fail("connection_error", str(exc), int((time.time() - start) * 1000))

    duration_ms = int((time.time() - start) * 1000)
    try:
        data = _final_result(proc.stdout) if images else json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        detail = (proc.stderr or proc.stdout or "empty output").strip()[:500]
        return _fail(_classify_error(detail), detail, duration_ms)

    if data.get("is_error") or proc.returncode != 0:
        detail = str(data.get("result") or proc.stderr or "claude -p failed")[:500]
        error_type = "rate_limit_error" if data.get("api_error_status") == 429 else _classify_error(detail)
        return _fail(error_type, detail, duration_ms)

    text = data.get("result") or ""
    tool_calls = []
    if tools:
        structured = data.get("structured_output")
        if not isinstance(structured, dict):
            try:
                structured = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                structured = {"text": text}
        text = structured.get("text") or ""
        known = {t.get("function", {}).get("name") for t in tools}
        for call in structured.get("tool_calls") or []:
            if call.get("name") in known:
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                    },
                })

    usage = data.get("usage") or {}
    cached = usage.get("cache_read_input_tokens", 0) or 0
    prompt_tokens = (usage.get("input_tokens", 0) or 0) + cached + (
        usage.get("cache_creation_input_tokens", 0) or 0)
    completion_tokens = usage.get("output_tokens", 0) or 0
    message: Dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "success": True,
        "response": {
            "model": model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "prompt_tokens_details": {"cached_tokens": cached},
            },
        },
        "duration_ms": duration_ms,
    }
