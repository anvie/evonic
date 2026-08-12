"""Codex Responses API client for OpenAI Codex subscription.

Uses httpx (same as OpenAI SDK) to bypass Cloudflare on chatgpt.com.
Header-based bypass: originator + ChatGPT-Account-ID.
SSE streaming for the Responses API.
Returns response dicts compatible with LLMClient.extract_content().
"""

import json
import logging
from typing import Any, Dict, Generator, List, Optional

import httpx

from backend.provider.oauth_codex import CODEX_BASE_URL, extract_account_id

_log = logging.getLogger(__name__)


def _classify_stream_error(code: str) -> str:
    """Map a Responses API error/incomplete code to an llm_loop error_type.

    'provider_error' is the transient default — llm_loop retries it with backoff
    and it is fallback-eligible, which is what we want for backend hiccups.
    """
    code = (code or "").lower()
    if any(k in code for k in ("rate_limit", "usage_limit", "quota", "too_many")):
        return "rate_limit_error"
    if any(k in code for k in ("auth", "unauthorized", "token_expired", "invalid_api_key")):
        return "auth_error"
    if "timeout" in code:
        return "request_timeout"
    if "max_output_tokens" in code or "max_tokens" in code:
        # Retrying the same request verbatim is unlikely to help — 'llm_error'
        # gives one retry, then falls back to another model.
        return "llm_error"
    return "provider_error"


def _map_usage(usage) -> Dict[str, Any]:
    """Map Responses API usage (input_tokens/output_tokens) to the
    OpenAI-chat-style keys (prompt_tokens/completion_tokens) that the rest
    of the pipeline (context monitor, traces, dashboards) reads."""
    if not isinstance(usage, dict) or not usage:
        return {}
    prompt = usage.get("input_tokens", 0) or 0
    completion = usage.get("output_tokens", 0) or 0
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    mapped = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": usage.get("total_tokens", 0) or (prompt + completion),
    }
    if input_details or output_details:
        mapped["prompt_tokens_details"] = {
            "cached_tokens": input_details.get("cached_tokens", 0) or 0,
        }
        mapped["completion_tokens_details"] = {
            "reasoning_tokens": output_details.get("reasoning_tokens", 0) or 0,
        }
    return mapped


class CodexClient:
    """Client for the OpenAI Codex Responses API (SSE transport)."""

    def __init__(self, access_token: str, base_url: str = ""):
        self.access_token = access_token
        self.base_url = (base_url or CODEX_BASE_URL).rstrip("/")
        self._account_id = extract_account_id(access_token)

    def _is_chatgpt_backend(self) -> bool:
        """True when talking to the ChatGPT subscription backend (Codex CLI protocol)."""
        return "chatgpt.com" in self.base_url

    def _headers(self, accept: str = "text/event-stream") -> Dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": accept,
            "User-Agent": "codex_cli_rs/0.0.0",
            "originator": "codex_cli_rs",
        }
        if self._account_id:
            h["ChatGPT-Account-ID"] = self._account_id
        return h

    def send_request(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        tools: Optional[List[Dict]] = None,
        reasoning: bool = False,
        stream: bool = True,
        timeout: int = 120,
        tool_choice: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a request to the Codex Responses API."""
        payload: Dict[str, Any] = {
            "model": model,
            "input": self._convert_messages(messages),
            "stream": stream,
            "store": False,
        }
        if reasoning:
            payload["reasoning"] = {"summary": "auto"}
        # Cap the output so a reasoning model can't spend the whole budget
        # thinking and return an empty (incomplete) response. The ChatGPT
        # Codex backend only accepts the parameter set the Codex CLI sends,
        # so the cap is limited to standard Responses API endpoints.
        if max_tokens and not self._is_chatgpt_backend():
            payload["max_output_tokens"] = max_tokens
        if tools:
            payload["tools"] = self._convert_tools(tools)
            if tool_choice:
                payload["tool_choice"] = {
                    "type": "function", "name": tool_choice}

        url = f"{self.base_url}/responses"

        try:
            if stream:
                return self._stream_response(url, payload, timeout)
            else:
                return self._blocking_response(url, payload, timeout)
        except httpx.TimeoutException:
            # Use 'request_timeout' (not 'timeout_error') so llm_loop retries on
            # the same model before falling back — reasoning turns are slow and a
            # single stall shouldn't demote us to a weaker fallback model.
            return {
                "success": False,
                "error_type": "request_timeout",
                "error": "Codex request timed out",
            }
        except httpx.ConnectError as e:
            return {
                "success": False,
                "error_type": "connection_error",
                "error": f"Cannot connect to Codex: {e}",
            }
        except Exception as e:
            _log.error("Codex request failed: %s", e, exc_info=True)
            return {
                "success": False,
                "error_type": "unknown_error",
                "error": str(e),
            }

    def _stream_response(self, url: str, payload: Dict, timeout: int) -> Dict[str, Any]:
        """Handle SSE streaming from the Responses API."""
        # Reasoning models can pause a long time before/between SSE chunks.
        # Keep connect fast, but allow a generous read-gap for reasoning.
        _timeout = httpx.Timeout(connect=30.0, read=max(timeout, 300), write=30.0, pool=30.0)
        with httpx.Client(timeout=_timeout) as client:
            with client.stream(
                "POST",
                url,
                json=payload,
                headers=self._headers(),
            ) as resp:
                if resp.status_code != 200:
                    body = resp.read().decode(errors="replace")[:500]
                    return {
                        "success": False,
                        "error_type": self._classify_http_error(resp.status_code),
                        "error": f"HTTP {resp.status_code}: {body}",
                    }

                content_parts = []
                thinking_parts = []
                tool_calls = []
                response_id = ""
                model_used = ""
                usage: Dict[str, Any] = {}
                # A stream can end in failure without an HTTP error — usage
                # limits, filtered output, or an output-token cap hit while
                # reasoning. Track it so an empty stream is reported as an
                # error instead of a successful empty answer (which the agent
                # loop renders as "(No response)").
                stream_error: Optional[Dict[str, str]] = None
                completed = False

                for line in resp.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break

                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type", "")

                    if event_type == "response.created":
                        r = event.get("response", {})
                        response_id = r.get("id", "")
                        model_used = r.get("model", "")

                    elif event_type == "response.output_text.delta":
                        content_parts.append(event.get("delta", ""))

                    elif event_type == "response.reasoning_summary_text.delta":
                        thinking_parts.append(event.get("delta", ""))

                    elif event_type == "response.reasoning_summary_part.added":
                        # Separate reasoning summary parts with a blank line
                        if thinking_parts:
                            thinking_parts.append("\n\n")

                    elif event_type == "response.output_item.done":
                        item = event.get("item", {})
                        if item.get("type") == "function_call":
                            tool_calls.append({
                                "id": item.get("call_id", ""),
                                "type": "function",
                                "function": {
                                    "name": item.get("name", ""),
                                    "arguments": item.get("arguments", "{}"),
                                },
                            })

                    elif event_type == "response.completed":
                        completed = True
                        r = event.get("response", {})
                        if not response_id:
                            response_id = r.get("id", "")
                        usage = _map_usage(r.get("usage"))

                    elif event_type == "response.failed":
                        r = event.get("response", {}) or {}
                        usage = _map_usage(r.get("usage")) or usage
                        err = r.get("error") or {}
                        code = err.get("code") or err.get("type") or ""
                        stream_error = {
                            "error_type": _classify_stream_error(code),
                            "error": (f"Codex response failed"
                                      f"{f' [{code}]' if code else ''}: "
                                      f"{err.get('message') or 'no detail'}"),
                        }

                    elif event_type == "response.incomplete":
                        r = event.get("response", {}) or {}
                        usage = _map_usage(r.get("usage")) or usage
                        reason = (r.get("incomplete_details") or {}).get("reason") or "unknown"
                        stream_error = {
                            "error_type": _classify_stream_error(reason),
                            "error": f"Codex response incomplete ({reason})",
                        }

                    elif event_type == "error":
                        err = event.get("error") if isinstance(event.get("error"), dict) else event
                        code = err.get("code") or err.get("type") or ""
                        stream_error = {
                            "error_type": _classify_stream_error(code),
                            "error": (f"Codex stream error"
                                      f"{f' [{code}]' if code else ''}: "
                                      f"{err.get('message') or 'no detail'}"),
                        }

        full_content = "".join(content_parts)
        full_thinking = "".join(thinking_parts)

        # Nothing usable came back — surface the failure so the agent loop can
        # retry or fall back, rather than treating it as an empty final answer.
        if not full_content and not tool_calls:
            if stream_error:
                _log.warning("Codex stream produced no output: %s", stream_error["error"])
                return {"success": False, **stream_error}
            if not completed:
                _log.warning("Codex stream ended without response.completed and no output")
                return {
                    "success": False,
                    "error_type": "provider_error",
                    "error": "Codex stream ended without a completed response",
                }
        elif stream_error:
            # Partial output is still usable — keep it, but leave a trace.
            _log.warning("Codex stream returned partial output: %s", stream_error["error"])

        message: Dict[str, Any] = {
            "role": "assistant",
            "content": full_content,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls
        if full_thinking:
            message["reasoning_content"] = full_thinking

        return {
            "success": True,
            "response": {
                "id": response_id,
                "model": model_used,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "stop" if not tool_calls else "tool_calls",
                    }
                ],
                "usage": usage,
            },
        }

    def _blocking_response(self, url: str, payload: Dict, timeout: int) -> Dict[str, Any]:
        """Non-streaming fallback."""
        payload["stream"] = False
        resp = httpx.post(
            url,
            json=payload,
            headers=self._headers(accept="application/json"),
            timeout=timeout,
        )

        if resp.status_code != 200:
            return {
                "success": False,
                "error_type": self._classify_http_error(resp.status_code),
                "error": f"HTTP {resp.status_code}: {resp.text[:500]}",
            }

        data = resp.json()
        output = data.get("output", [])
        content = ""
        thinking = ""
        tool_calls = []

        for item in output:
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        content += part.get("text", "")
            elif item.get("type") == "reasoning":
                for part in item.get("summary", []):
                    if part.get("type") == "summary_text":
                        thinking += part.get("text", "")
            elif item.get("type") == "function_call":
                tool_calls.append({
                    "id": item.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                })

        # Same guard as the streaming path: a failed/incomplete response with no
        # output must not look like a successful empty answer.
        status = data.get("status", "")
        if not content and not tool_calls and status in ("failed", "incomplete", "cancelled"):
            if status == "incomplete":
                code = (data.get("incomplete_details") or {}).get("reason") or "unknown"
                detail = f"Codex response incomplete ({code})"
            else:
                err = data.get("error") or {}
                code = err.get("code") or err.get("type") or status
                detail = f"Codex response {status} [{code}]: {err.get('message') or 'no detail'}"
            _log.warning("Codex response produced no output: %s", detail)
            return {
                "success": False,
                "error_type": _classify_stream_error(code),
                "error": detail,
            }

        message: Dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        if thinking:
            message["reasoning_content"] = thinking

        return {
            "success": True,
            "response": {
                "id": data.get("id", ""),
                "model": data.get("model", ""),
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "stop" if not tool_calls else "tool_calls",
                    }
                ],
                "usage": _map_usage(data.get("usage")),
            },
        }

    def stream_chunks(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        tools: Optional[List[Dict]] = None,
        reasoning: bool = False,
        timeout: int = 120,
    ) -> Generator[Dict[str, Any], None, None]:
        """Yield SSE delta chunks for real-time streaming to the frontend."""
        payload: Dict[str, Any] = {
            "model": model,
            "input": self._convert_messages(messages),
            "stream": True,
            "store": False,
        }
        if reasoning:
            payload["reasoning"] = {"summary": "auto"}
        if tools:
            payload["tools"] = self._convert_tools(tools)

        url = f"{self.base_url}/responses"

        _timeout = httpx.Timeout(connect=30.0, read=max(timeout, 300), write=30.0, pool=30.0)
        with httpx.Client(timeout=_timeout) as client:
            with client.stream(
                "POST",
                url,
                json=payload,
                headers=self._headers(),
            ) as resp:
                if resp.status_code != 200:
                    yield {
                        "error": True,
                        "error_type": self._classify_http_error(resp.status_code),
                        "message": f"HTTP {resp.status_code}",
                    }
                    return

                for line in resp.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        event = json.loads(data_str)
                        yield event
                    except json.JSONDecodeError:
                        continue

    def test_connection(self) -> Dict[str, Any]:
        """Test if the Codex endpoint is reachable with current token."""
        try:
            resp = httpx.get(
                f"{self.base_url}/models",
                headers=self._headers(accept="application/json"),
                params={"client_version": "1.0.0"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                models = data.get("data", data.get("models", []))
                return {
                    "success": True,
                    "message": "Connected to Codex",
                    "available_models": len(models) if isinstance(models, list) else 0,
                }
            return {
                "success": False,
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def _convert_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert Chat Completions messages to Responses API input format."""
        converted = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                converted.append({
                    "role": "system",
                    "content": content if isinstance(content, str) else json.dumps(content),
                })
            elif role == "user":
                if isinstance(content, list):
                    # Convert Chat Completions content block types to Responses API format.
                    # Chat Completions: "text" / "image_url" → Responses: "input_text" / "input_image"
                    converted_content = []
                    for block in content:
                        btype = block.get("type", "")
                        if btype == "text":
                            block = {**block, "type": "input_text"}
                        elif btype == "image_url":
                            # Chat Completions: {"image_url": {"url": "..."}}
                            # Responses API:    {"image_url": "..."}
                            url = block.get("image_url", {})
                            if isinstance(url, dict):
                                url = url.get("url", "")
                            block = {"type": "input_image", "image_url": url}
                        converted_content.append(block)
                    converted.append({"role": "user", "content": converted_content})
                else:
                    converted.append({"role": "user", "content": str(content)})
            elif role == "assistant":
                # Keep the assistant's own text even when the same message
                # carries tool calls — it is part of the reasoning trail the
                # model needs on the next turn. Empty content is dropped
                # entirely (the Responses API has no use for a blank item).
                text = "" if content is None else (
                    content if isinstance(content, str) else json.dumps(content)
                )
                if text.strip():
                    converted.append({"role": "assistant", "content": text})
                for tc in msg.get("tool_calls") or []:
                    converted.append({
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": tc["function"]["name"],
                        "arguments": tc["function"].get("arguments", "{}"),
                    })
            elif role == "tool":
                converted.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": str(content),
                })

        return converted

    @staticmethod
    def _convert_tools(tools: List[Dict]) -> List[Dict]:
        """Convert Chat Completions tools to Responses API format.

        Chat Completions nests under "function":
            {"type": "function", "function": {"name", "description", "parameters"}}
        Responses API is flat:
            {"type": "function", "name", "description", "parameters", "strict": False}
        """
        converted = []
        for t in tools:
            if t.get("type") == "function" and "function" in t:
                fn = t["function"]
                converted.append({
                    "type": "function",
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                    "strict": False,
                })
            else:
                converted.append(t)
        return converted

    @staticmethod
    def _classify_http_error(status_code: int) -> str:
        if status_code == 401:
            return "auth_error"
        elif status_code == 429:
            return "rate_limit_error"
        elif status_code >= 500:
            return "api_error"
        elif status_code == 408:
            return "timeout_error"
        return "llm_error"
