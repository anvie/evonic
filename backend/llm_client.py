"""LLM client module for OpenAI-compatible API interactions.

Provides the LLMClient class for chat completions, thinking tag handling,
and response parsing. Supports multiple model backends and formats
including standard XML thinking tags, Gemma 4, and Qwen formats.

"""

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

import config
from backend.normalizer import normalize_llm_text
from evaluator.api_logger import log_api_call
from evaluator.gemma4_parser import is_gemma4_format, strip_gemma4_thinking

# ── Centralized error formatting ──────────────────────────────────────────────

_LLM_ERROR_MESSAGES = {
    "api_error": "The LLM API is temporarily unavailable.",
    "rate_limit_error": "API rate limit exceeded. Please wait and try again.",
    "auth_error": "API authentication failed. Check your API key.",
    "timeout_error": "Request timed out. The service may be slow or unavailable.",
    "request_timeout": "Request timed out. The service may be slow or unavailable.",
    "connection_error": "Cannot connect to the LLM server.",
    "context_length_error": "Conversation too long. Start a new session.",
    "generation_timeout": "The AI ran out of tokens before finishing. Try a shorter conversation.",
    "tool_call_json_error": "The AI generated an invalid tool call. Retrying with a correction.",
    "provider_error": "The LLM provider is experiencing issues. Please try again shortly.",
    "llm_error": "The LLM returned an error. Please try again.",
    "unknown_error": "An unexpected error occurred with the LLM service.",
}


def _format_llm_error(error_type: str, context: Optional[Dict[str, Any]] = None) -> str:
    """Format an LLM error type into a user-friendly message.

    Args:
        error_type: Internal error classification (e.g. 'api_error', 'timeout').
        context: Optional dict for appending non-sensitive context (e.g. session_id).

    Returns:
        User-friendly error string — never exposes API keys or raw API responses.
    """
    user_msg = _LLM_ERROR_MESSAGES.get(error_type, _LLM_ERROR_MESSAGES["unknown_error"])
    if context:
        if context.get("session_id"):
            user_msg += f" (Session: {context['session_id']})"
    return user_msg


def _split_trailing_think_close(text: str) -> Tuple[str, Optional[str]]:
    """Split text on </think> marker — returns (actual_thinking, trailing_final_response).

    Some backends accidentally include </think> and the final response inside
    the reasoning_content field. This extracts the trailing response.
    Returns (original_text, None) if no </think> found or nothing follows it.
    """
    if not text or "</think>" not in text:
        return text, None
    parts = text.split("</think>", 1)
    actual = parts[0].strip()
    trailing = parts[1].strip() if len(parts) > 1 else ""
    return actual or text, trailing or None


def strip_thinking_tags(content: str) -> Tuple[str, Optional[str]]:
    """
    Strip thinking tags from content with auto-format detection.

    Supports:
    - Standard: <think>...</think>
    - Gemma 4: <|channel>thought...<channel|>

    Returns:
        Tuple of (cleaned_content, thinking_content)
    """
    if not content:
        return content, None

    if is_gemma4_format(content):
        return strip_gemma4_thinking(content)

    thinking_pattern = r"<think>(.*?)</think>"
    thinking_matches = re.findall(thinking_pattern, content, re.DOTALL)
    cleaned = re.sub(thinking_pattern, "", content, flags=re.DOTALL).strip()
    thinking_content = "\n".join(thinking_matches) if thinking_matches else None

    # Edge case: model put the final response inside <think>...</think>, leaving cleaned empty.
    # Check if thinking_content itself has an embedded </think> that signals end-of-thinking.
    if not cleaned and thinking_content:
        actual_thinking, embedded_final = _split_trailing_think_close(thinking_content)
        if embedded_final:
            return embedded_final, actual_thinking

    # Fallback: handle missing opening <think> tag (common with vLLM)
    if not thinking_content and "</think>" in content:
        parts = content.split("</think>", 1)
        thinking_text = parts[0].strip()
        cleaned_text = parts[1].strip() if len(parts) > 1 else ""
        if thinking_text:
            return cleaned_text or "No content generated", thinking_text
        return cleaned_text or content.replace("</think>", "").strip(), None

    return cleaned, thinking_content


def _convert_image_url_to_claude(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert OpenAI-style image_url content blocks to Anthropic image+source format."""
    result = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue
        new_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url.startswith("data:"):
                    # data:<media_type>;base64,<data>
                    try:
                        header, b64data = url.split(",", 1)
                        media_type = header.split(":")[1].split(";")[0]
                    except (ValueError, IndexError):
                        media_type, b64data = "image/jpeg", url
                    new_parts.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64data},
                    })
                else:
                    new_parts.append({
                        "type": "image",
                        "source": {"type": "url", "url": url},
                    })
            else:
                new_parts.append(part)
        result.append({**msg, "content": new_parts})
    return result


class LLMClient:
    """Client for OpenAI-compatible LLM chat completion APIs.

    Handles authentication, retry logic with exponential backoff,
    thinking/reasoning tag extraction, and response parsing.
    Supports llama.cpp, OpenAI, and other OpenAI-compatible backends.
    """

    def __init__(self, model_config: Optional[Dict[str, Any]] = None):
        """Initialize LLMClient with optional model_config.

        Args:
            model_config: Dict with keys: base_url, api_key, model_name, timeout,
                         thinking (bool), thinking_budget (int), max_tokens, temperature.
                         If None, uses the default model from DB or config.py defaults.
        """
        if model_config:
            self.base_url = model_config.get("base_url")
            self.api_key = model_config.get("api_key")
            self.model = model_config.get("model_name")
            self.timeout = model_config.get("timeout")
            self.thinking = model_config.get("thinking", False)
            self.thinking_budget = model_config.get("thinking_budget", 0)
            self.max_tokens = model_config.get("max_tokens")
            self.temperature = model_config.get("temperature")
            self.api_format = model_config.get("api_format", "openai")
        else:
            try:
                from models.db import db

                dm = db.get_default_model()
                if dm:
                    self.base_url = dm.get("base_url")
                    self.api_key = dm.get("api_key")
                    self.model = dm.get("model_name")
                    self.timeout = dm.get("timeout")
                    self.thinking = bool(dm.get("thinking", False))
                    self.thinking_budget = int(dm.get("thinking_budget", 0))
                    self.max_tokens = dm.get("max_tokens")
                    self.temperature = dm.get("temperature")
                    self.api_format = dm.get("api_format", "openai")
                else:
                    self.base_url = None
                    self.api_key = None
                    self.model = None
                    self.timeout = None
                    self.thinking = False
                    self.thinking_budget = 0
                    self.max_tokens = None
                    self.temperature = None
                    self.api_format = "openai"
            except Exception:
                self.base_url = None
                self.api_key = None
                self.model = None
                self.timeout = None
                self.thinking = False
                self.thinking_budget = 0
                self.max_tokens = None
                self.temperature = None
                self.api_format = "openai"
        self._cached_model_name = None
        # Cache for global LLM settings (avoids repeated DB reads in hot path).
        # TTL-based, simple dict — intentionally lock-free (worst case: 1 extra DB read).
        self._settings_cache = {}
        self._settings_cache_time = 0

    def _get_cached_setting(self, cache_key: str, db_func, *args) -> Any:
        """Return a cached setting value with a 30-second TTL.

        On cache miss or TTL expiry, calls ``db_func(*args)`` and stores the
        result (including None) so absent settings don't trigger repeated DB reads.
        """
        now = time.time()
        if now - self._settings_cache_time > 30:
            self._settings_cache = {}
            self._settings_cache_time = now
        if cache_key not in self._settings_cache:
            self._settings_cache[cache_key] = db_func(*args)
        return self._settings_cache[cache_key]

    def get_actual_model_name(self, force_refresh: bool = False) -> str:
        """Get the actual model name from the remote endpoint.

        For llama.cpp servers, fetches from /props endpoint.
        Falls back to configured model name if endpoint is unavailable.
        """
        if self._cached_model_name and not force_refresh:
            return self._cached_model_name

        try:
            props_url = f"{self.base_url.rstrip('/v1')}/props"
            response = requests.get(props_url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                if "model_alias" in data:
                    self._cached_model_name = data["model_alias"]
                    return self._cached_model_name
        except Exception:
            pass

        # Only trust /v1/models when exactly one model is returned (local servers).
        try:
            models_url = f"{self.base_url}/models"
            response = requests.get(models_url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                models = data.get("data") or data.get("models") or []
                if len(models) == 1:
                    key = "id" if "id" in models[0] else "name"
                    self._cached_model_name = models[0].get(key, self.model)
                    return self._cached_model_name
        except Exception:
            pass

        return self.model

    def test_connection(self) -> Dict[str, Any]:
        """Test connection to the model endpoint."""
        try:
            if self.api_format == "ollama":
                models_url = f"{self.base_url}/tags"
            else:
                models_url = f"{self.base_url}/v1/models"
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            response = requests.get(models_url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if self.api_format == "ollama":
                    models_list = data.get("models") or []
                else:
                    models_list = data.get("data") or data.get("models") or []
                return {
                    "success": True,
                    "message": f"Connected to {self.base_url}",
                    "available_models": len(models_list),
                }
            return {"success": False, "error": _format_llm_error("api_error")}
        except requests.exceptions.Timeout:
            return {"success": False, "error": _format_llm_error("timeout_error")}
        except requests.exceptions.ConnectionError as e:
            return {"success": False, "error": _format_llm_error("connection_error")}
        except Exception as e:
            return {"success": False, "error": _format_llm_error("unknown_error")}

    def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        enable_thinking: bool = True,
        max_tokens: Optional[int] = None,
        log_file: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send chat completion request to OpenAI-compatible endpoint.

        Processes messages (normalizes quotes, injects thinking prompts for
        Gemma 4, merges multiple system messages), applies token budget
        adjustments for thinking mode, and retries on transient errors
        with exponential backoff.

        Args:
            messages: List of message dicts with role and content keys.
            tools: Optional list of tool definitions for function calling.
            temperature: Optional override for model temperature.
            enable_thinking: If True and model supports thinking, enables
                reasoning mode. Defaults to True.
            max_tokens: Optional override for max output tokens. If None,
                uses self.max_tokens (doubled when thinking is active).
            log_file: Optional path for API call logging.

        Returns:
            Dict with response, duration_ms, token counts, success flag,
            and error_type/error_detail on failure.

        Note:
            Retries on 5xx errors, timeouts, and connection errors with
            exponential backoff (max 60s between retries). Configurable
            retry count via llm_max_retries setting (DB default: 5).
        """
        is_ollama_fmt = self.api_format == "ollama" or (
            self.base_url and "ollama.com" in self.base_url
        )
        is_claude = (self.model or "").lower().startswith("claude") or (
            self.base_url and "anthropic.com" in self.base_url
        )
        url = (
            f"{self.base_url}/chat"
            if is_ollama_fmt
            else f"{self.base_url}/chat/completions"
        )
        if max_tokens is None:
            max_tokens = self.max_tokens
        # When thinking is active, the model's internal chain-of-thought consumes tokens
        # from the same max_tokens budget before producing any output. Double the budget
        # so actual output isn't crowded out by heavy reasoning. Only applied when thinking
        # is enabled and no explicit thinking_budget cap is configured (thinking_budget > 0
        # means the caller already sized the budget intentionally).
        if self.thinking and enable_thinking and not self.thinking_budget:
            max_tokens = max_tokens * 2
        try:
            from models.db import db as _db

            _ctx_len = int(self._get_cached_setting("llm_context_length", _db.get_setting, "llm_context_length", 0) or 0)
            _prompt_buf = int(self._get_cached_setting("llm_prompt_buffer", _db.get_setting, "llm_prompt_buffer", 2048) or 2048)
            if _ctx_len > 0:
                max_tokens = min(max_tokens, _ctx_len - _prompt_buf)
        except Exception:
            pass

        # Caller > model setting > omit (let server decide)
        effective_temperature = (
            temperature if temperature is not None else self.temperature
        )

        # Normalize quote-like characters in all outgoing messages so the LLM is
        # less likely to reproduce them verbatim inside tool call argument strings,
        # which would cause llama.cpp --jinja to fail JSON parsing.
        processed_messages = []
        thinking_injected = False
        actual_model = self._cached_model_name or self.model or ""
        model_lower = actual_model.lower()
        is_gemma4 = (
            "gemma-4" in model_lower
            or "gemma4" in model_lower
            or "gemma-4-base" in model_lower
        )

        for msg in messages:
            new_msg = msg.copy()
            if isinstance(new_msg.get("content"), str):
                new_msg["content"] = normalize_llm_text(new_msg["content"])
            if not thinking_injected and is_gemma4 and enable_thinking:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role in ("user", "system") and content:
                    new_msg["content"] = "<|think|>\n" + new_msg["content"]
                    thinking_injected = True
            processed_messages.append(new_msg)

        # Merge multiple leading system messages into one to satisfy strict chat
        # templates (e.g. Llama 3.x) that only allow a single system message.
        n_sys = 0
        for m in processed_messages:
            if m.get("role") == "system":
                n_sys += 1
            else:
                break
        if n_sys > 1:
            combined_content = "\n\n".join(
                m.get("content", "") for m in processed_messages[:n_sys]
            )
            merged = processed_messages[0].copy()
            merged["content"] = combined_content
            processed_messages = [merged] + processed_messages[n_sys:]

        # Handle reasoning_content field based on thinking mode.
        # Some models (e.g. DeepSeek-v4) produce reasoning_content automatically
        # even without explicit thinking mode. Detect this by checking if any
        # assistant message already carries reasoning_content — if so, preserve
        # it so the API receives it back on the next call.
        _has_reasoning = any(
            _msg.get("reasoning_content")
            for _msg in processed_messages
            if _msg.get("role") == "assistant"
        )
        if self.thinking or _has_reasoning:
            # Ensure every assistant message has the field (some APIs require it
            # even on turns where the model produced no reasoning).
            for _msg in processed_messages:
                if _msg.get("role") == "assistant" and "reasoning_content" not in _msg:
                    _msg["reasoning_content"] = ""
        else:
            # No thinking configured and no reasoning in history — strip the
            # field so APIs that reject unknown fields are not affected.
            for _msg in processed_messages:
                _msg.pop("reasoning_content", None)

        # Claude API uses {"type":"image","source":{...}} instead of OpenAI's image_url format.
        if is_claude:
            processed_messages = _convert_image_url_to_claude(processed_messages)

        if is_ollama_fmt:
            payload = {
                "model": self.model,
                "messages": processed_messages,
                "stream": False,
                "options": {},
            }
            if max_tokens is not None:
                payload["options"]["num_predict"] = max_tokens
            if effective_temperature is not None:
                payload["options"]["temperature"] = effective_temperature
            if tools:
                payload["tools"] = tools
        else:
            payload = {
                "model": self.model,
                "messages": processed_messages,
                "max_tokens": max_tokens,
                "stream": False,
            }
            if effective_temperature is not None:
                payload["temperature"] = effective_temperature
            if tools:
                payload["tools"] = tools
            if self.thinking and enable_thinking:
                budget = (
                    self.thinking_budget
                    if self.thinking_budget > 0
                    else max_tokens // 2
                )
                payload["thinking"] = {"type": "enabled", "budget_tokens": budget}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            from models.db import db as _db

            _val = self._get_cached_setting("llm_max_retries", _db.get_setting, "llm_max_retries", None)
            max_retries = int(_val) if _val is not None else 5
        except Exception:
            max_retries = 5
        last_error_result = None

        for attempt in range(1 + max_retries):
            try:
                start_time = time.time()
                response = requests.post(
                    url, json=payload, headers=headers, timeout=(10, self.timeout)
                )
                duration_ms = int((time.time() - start_time) * 1000)

                if response.status_code >= 500:
                    raw_text = response.text
                    # llama.cpp returns 500 when it cannot parse tool call arguments
                    # as JSON (e.g. unescaped quotes from Jinja templates). Retrying
                    # regenerates the identical broken call — return a distinct error
                    # type so the caller can inject a correction prompt instead.
                    if "Failed to parse tool call arguments" in raw_text:
                        error_msg = f"LLM API server error: {response.status_code} - {raw_text[:300]}"
                        log_api_call(
                            messages,
                            None,
                            duration_ms,
                            error=error_msg,
                            log_file=log_file,
                        )
                        user_msg = _format_llm_error("tool_call_json_error")
                        return {
                            "response": {"error": user_msg},
                            "duration_ms": duration_ms,
                            "success": False,
                            "error_type": "tool_call_json_error",
                            "error_detail": error_msg,
                        }
                    # Generic server error — retryable with exponential backoff
                    error_msg = f"LLM API server error: {response.status_code} - {raw_text[:200]}"
                    log_api_call(
                        messages,
                        None,
                        duration_ms,
                        error=f"[attempt {attempt + 1}/{1 + max_retries}] {error_msg}",
                        log_file=log_file,
                    )
                    last_error_result = {
                        "response": {"error": _format_llm_error("api_error")},
                        "duration_ms": duration_ms,
                        "success": False,
                        "error_type": "api_error",
                        "error_detail": error_msg,
                    }
                    if attempt < max_retries:
                        time.sleep(min(2 ** (attempt + 1), 60))
                        continue
                    return last_error_result

                if response.status_code != 200:
                    error_msg = (
                        f"LLM API error: {response.status_code} - {response.text[:200]}"
                    )
                    log_api_call(
                        messages, None, duration_ms, error=error_msg, log_file=log_file
                    )
                    return {
                        "response": {"error": _format_llm_error("api_error")},
                        "duration_ms": duration_ms,
                        "success": False,
                        "error_type": "api_error",
                        "error_detail": error_msg,
                    }

                result = response.json()

                # Transform Ollama native response to OpenAI-compatible format
                if is_ollama_fmt:
                    ollama_message = result.get("message", {})
                    ollama_content = ollama_message.get("content", "")
                    ollama_reasoning = ollama_message.get("reasoning_content", "")
                    prompt_eval = result.get("prompt_eval_count", 0)
                    eval_count = result.get("eval_count", 0)
                    transformed_message = {
                        "role": "assistant",
                        "content": ollama_content,
                    }
                    if ollama_reasoning:
                        transformed_message["reasoning_content"] = ollama_reasoning
                    result = {
                        "choices": [
                            {
                                "message": transformed_message,
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": prompt_eval,
                            "completion_tokens": eval_count,
                            "total_tokens": prompt_eval + eval_count,
                        },
                    }

                if "error" in result:
                    error_obj = result.get("error", {})
                    error_code = (
                        error_obj.get("code") if isinstance(error_obj, dict) else None
                    )
                    error_msg_str = (
                        error_obj.get("message", "")
                        if isinstance(error_obj, dict)
                        else str(error_obj)
                    ).lower()
                    is_transient = (
                        (isinstance(error_code, int) and error_code >= 500)
                        or str(error_code) in ("429", "503", "502", "529", "500")
                        or "provider" in error_msg_str
                        or "overloaded" in error_msg_str
                        or "unavailable" in error_msg_str
                    )
                    error_detail = str(error_obj)
                    log_api_call(
                        messages,
                        None,
                        duration_ms,
                        error=f"[attempt {attempt + 1}/{1 + max_retries}] {error_detail}"
                        if is_transient
                        else error_detail,
                        log_file=log_file,
                    )
                    _et = "provider_error" if is_transient else "llm_error"
                    last_error_result = {
                        "response": result,
                        "duration_ms": duration_ms,
                        "success": False,
                        "error_type": _et,
                        "error_detail": error_detail,
                    }
                    if is_transient and attempt < max_retries:
                        time.sleep(min(2 ** (attempt + 1), 60))
                        continue
                    last_error_result["response"] = {"error": _format_llm_error(_et)}
                    return last_error_result

                choices = result.get("choices", [])
                if choices:
                    finish_reason = choices[0].get("finish_reason")
                    message = choices[0].get("message", {})
                    content = message.get("content", "")
                    reasoning = (
                        message.get("reasoning_content")
                        or message.get("reasoning")
                        or ""
                    )

                    if finish_reason == "length" and not content:
                        error_detail = f"Generation hit max_tokens limit ({payload['max_tokens']}) without producing final answer. Reasoning length: {len(reasoning)} chars."
                        log_api_call(
                            messages,
                            None,
                            duration_ms,
                            error=error_detail,
                            log_file=log_file,
                        )
                        user_msg = _format_llm_error("generation_timeout")
                        return {
                            "response": {"error": user_msg},
                            "duration_ms": duration_ms,
                            "success": False,
                            "error_type": "generation_timeout",
                            "error_detail": error_detail,
                        }

                usage = result.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                total_tokens = usage.get(
                    "total_tokens", prompt_tokens + completion_tokens
                )

                response_text = ""
                thinking_text = ""
                if choices:
                    msg = choices[0].get("message", {})
                    raw_content = msg.get("content", "") or ""
                    thinking_text = (
                        msg.get("reasoning_content") or msg.get("reasoning") or ""
                    )
                    if thinking_text:
                        response_text = raw_content
                    else:
                        response_text, thinking_text = strip_thinking_tags(raw_content)
                        thinking_text = thinking_text or ""
                log_api_call(
                    messages,
                    response_text,
                    duration_ms,
                    log_file=log_file,
                    thinking=thinking_text or None,
                )

                return {
                    "response": result,
                    "duration_ms": duration_ms,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "success": True,
                }

            except requests.exceptions.Timeout:
                elapsed_ms = int((time.time() - start_time) * 1000)
                log_api_call(
                    messages,
                    None,
                    elapsed_ms,
                    error=f"[attempt {attempt + 1}/{1 + max_retries}] Timeout after {self.timeout}s",
                    log_file=log_file,
                )
                last_error_result = {
                    "response": {"error": _format_llm_error("request_timeout")},
                    "duration_ms": elapsed_ms,
                    "success": False,
                    "error_type": "request_timeout",
                    "error_detail": f"HTTP request timed out after {self.timeout} seconds.",
                }
                if attempt < max_retries:
                    time.sleep(min(2 ** (attempt + 1), 60))
                    continue
                return last_error_result

            except requests.exceptions.ConnectionError as e:
                log_api_call(
                    messages,
                    None,
                    0,
                    error=f"[attempt {attempt + 1}/{1 + max_retries}] Connection error: {str(e)[:100]}",
                    log_file=log_file,
                )
                last_error_result = {
                    "response": {"error": _format_llm_error("connection_error")},
                    "duration_ms": 0,
                    "success": False,
                    "error_type": "connection_error",
                    "error_detail": f"Could not connect to LLM server at {self.base_url}.",
                }
                if attempt < max_retries:
                    time.sleep(min(2 ** (attempt + 1), 60))
                    continue
                return last_error_result

            except Exception as e:
                elapsed_ms = (
                    int((time.time() - start_time) * 1000)
                    if "start_time" in locals()
                    else 0
                )
                log_api_call(
                    messages, None, elapsed_ms, error=str(e)[:200], log_file=log_file
                )
                return {
                    "response": {"error": _format_llm_error("unknown_error")},
                    "duration_ms": elapsed_ms,
                    "success": False,
                    "error_type": "unknown_error",
                    "error_detail": str(e),
                }

        return last_error_result

    def extract_content(
        self, response: Dict[str, Any], strip_thinking: bool = True
    ) -> str:
        """Extract text content from LLM response."""
        if not response.get("success"):
            error_type = response.get("error_type", "unknown_error")
            user_msg = _format_llm_error(error_type)
            # Include error_detail only for internal/admin contexts — never raw API responses
            error_detail = response.get("error_detail", "")
            if error_detail:
                return f"{user_msg}\n\nDetails: {error_detail}"
            return user_msg

        choices = response["response"].get("choices", [])
        if not choices:
            return "No response generated"

        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls")
        if tool_calls:
            return json.dumps({"tool_calls": tool_calls}, indent=2)

        content = message.get("content", "")
        if not content:
            content = message.get("reasoning_content", "")
        if not content:
            return "No content generated"

        if strip_thinking:
            cleaned, _ = strip_thinking_tags(content)
            return cleaned
        return content

    def extract_content_with_thinking(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Extract both thinking and final content from LLM response.

        Handles:
        1. llama.cpp --reasoning mode: thinking in message.reasoning_content
        2. Tag-based thinking: <think>...</think> or Gemma4/Qwen XML formats
        """
        if not response.get("success"):
            return {
                "content": self.extract_content(response),
                "thinking": None,
                "raw": None,
            }

        choices = response["response"].get("choices", [])
        if not choices:
            return {"content": "No response generated", "thinking": None, "raw": None}

        message = choices[0].get("message", {})
        content = message.get("content", "")
        reasoning_content = message.get("reasoning_content") or message.get("reasoning")
        tool_calls = message.get("tool_calls")

        if tool_calls:
            tool_content = json.dumps({"tool_calls": tool_calls}, indent=2)
            return {
                "content": tool_content,
                "thinking": (reasoning_content or "").strip() or None,
                "raw": tool_content,
                "tool_calls": tool_calls,
            }

        if content and "<|tool_call>" in content:
            from evaluator.gemma4_parser import (
                extract_gemma4_tool_calls,
                gemma4_tool_calls_to_openai_format,
            )

            gemma4_calls = extract_gemma4_tool_calls(content)
            if gemma4_calls:
                openai_calls = gemma4_tool_calls_to_openai_format(gemma4_calls)
                tool_content = json.dumps({"tool_calls": openai_calls}, indent=2)
                return {
                    "content": tool_content,
                    "thinking": reasoning_content,
                    "raw": content,
                    "tool_calls": openai_calls,
                }

        if content and "<tool_call>" in content:
            from evaluator.qwen_parser import (
                extract_qwen_tool_calls,
                qwen_tool_calls_to_openai_format,
                strip_qwen_tool_calls,
            )

            qwen_calls = extract_qwen_tool_calls(content)
            if qwen_calls:
                openai_calls = qwen_tool_calls_to_openai_format(qwen_calls)
                visible_content = strip_qwen_tool_calls(content)
                return {
                    "content": visible_content,
                    "thinking": (reasoning_content or "").strip() or None,
                    "raw": content,
                    "tool_calls": openai_calls,
                }

        reasoning_text = (reasoning_content or "").strip()
        embedded_final = None
        if reasoning_text and "</think>" in reasoning_text:
            reasoning_text, embedded_final = _split_trailing_think_close(reasoning_text)
        if reasoning_text:
            cleaned = strip_thinking_tags(content)[0] if content else ""
            if not cleaned and embedded_final:
                cleaned = embedded_final
            # Check for Qwen-style XML tool calls that may appear in
            # reasoning_content instead of content (common with Qwen-based models).
            # Two forms: (a) trailing after </think> in embedded_final,
            # (b) directly in reasoning_text when content is empty.
            xml_source = None
            if embedded_final and "<tool_call>" in embedded_final:
                xml_source = embedded_final
            elif not cleaned and reasoning_text and "<tool_call>" in reasoning_text:
                xml_source = reasoning_text
            if xml_source:
                from evaluator.qwen_parser import (
                    extract_qwen_tool_calls,
                    qwen_tool_calls_to_openai_format,
                    strip_qwen_tool_calls,
                )
                qwen_calls = extract_qwen_tool_calls(xml_source)
                if qwen_calls:
                    openai_calls = qwen_tool_calls_to_openai_format(qwen_calls)
                    visible_content = strip_qwen_tool_calls(xml_source)
                    return {
                        "content": visible_content,
                        "thinking": reasoning_text or None,
                        "raw": content,
                        "tool_calls": openai_calls,
                    }
            return {"content": cleaned, "thinking": reasoning_text, "raw": content}

        if content:
            cleaned, thinking = strip_thinking_tags(content)
            return {"content": cleaned, "thinking": thinking, "raw": content}

        return {"content": "No content generated", "thinking": None, "raw": None}

    def get_error_info(self, response: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Get error information from a failed response."""
        if response.get("success"):
            return None
        return {
            "type": response.get("error_type", "unknown"),
            "message": response["response"].get("error", "Unknown error")
            if isinstance(response.get("response"), dict)
            else str(response.get("response")),
            "detail": response.get("error_detail", ""),
            "duration_ms": response.get("duration_ms", 0),
        }

    def extract_tool_calls(
        self, response: Dict[str, Any]
    ) -> Optional[List[Dict[str, Any]]]:
        """Extract tool calls from LLM response."""
        if not response.get("success"):
            return None
        choices = response["response"].get("choices", [])
        if not choices:
            return None
        message = choices[0].get("message", {})
        return message.get("tool_calls")


# Global LLM client instance (initialized once at startup, uses DB default model)
llm_client = LLMClient()


def get_llm_client() -> LLMClient:
    """Create a fresh LLMClient that reads the latest default model from DB.

    Use this in request handlers when you need the current default model
    without restarting the server.  Each call creates a new LLMClient
    instance that queries db.get_default_model() at init time.
    """
    return LLMClient()
