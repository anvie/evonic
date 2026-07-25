"""
LLM usage telemetry — a generic, plugin-agnostic hook for observing token usage.

This module is core infrastructure, NOT tied to any specific plugin. It does two
generic things any observer can build on:

  1. ``usage_context(...)`` — a context manager that annotates the *current* LLM
     call with metadata (source, agent, session). Any caller (core or plugin) may
     set it; any observer may read it. Annotation is optional — unannotated calls
     emit with ``source='other'``.

  2. ``record_llm_usage(...)`` — called once per successful LLM completion. It
     fills in token counts (estimating via tiktoken when the provider returns no
     ``usage``) and emits a generic ``llm_usage`` event on the shared event bus.

Consumers (cost dashboards, rate limiters, audit logs, anomaly detectors) simply
subscribe to the ``llm_usage`` event via the standard plugin manifest — exactly
like ``turn_complete``. Removing any such plugin leaves this module standalone.
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)

# Per-thread/per-context annotation for the LLM call currently in flight.
_usage_ctx: ContextVar[Optional[Dict[str, Any]]] = ContextVar('llm_usage_ctx', default=None)


@contextmanager
def usage_context(source: str,
                  agent_id: Optional[str] = None,
                  agent_name: Optional[str] = None,
                  session_id: Optional[str] = None):
    """Annotate LLM calls made within this block with their originating source.

    Generic — used by core call sites (agent turns, summarizer, memory) and
    available to plugins too. Restores the previous context on exit.
    """
    token = _usage_ctx.set({
        'source': source,
        'agent_id': agent_id,
        'agent_name': agent_name,
        'session_id': session_id,
    })
    try:
        yield
    finally:
        _usage_ctx.reset(token)


# ── tiktoken token counter (reuses the cl100k_base pattern used elsewhere) ──
_tiktoken_enc = None


def estimate_tokens(text: str) -> int:
    """Estimate token count via tiktoken cl100k_base. Falls back to len//4.

    Mirrors backend/agent_runtime/llm_loop.py:_count_tokens — used only when a
    provider response omits a usage block.
    """
    global _tiktoken_enc
    if not text:
        return 0
    try:
        if _tiktoken_enc is None:
            import tiktoken
            _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        return len(_tiktoken_enc.encode(text))
    except Exception:
        return len(text) // 4


def estimate_context_tokens(messages: Optional[List[Dict[str, Any]]],
                            tools: Optional[List[Dict[str, Any]]] = None) -> int:
    """Estimate the prompt-side token count of a request from the exact
    messages and tool definitions sent. Used by the context monitor when the
    provider returns no usage block (e.g. the Codex Responses endpoint).

    Counts message text, tool_calls arguments, and the tool schema JSON —
    the three things that actually dominate the context window.
    """
    import json as _json
    total = estimate_tokens(_flatten_messages(messages))
    for m in (messages or []):
        if not isinstance(m, dict):
            continue
        for tc in (m.get("tool_calls") or []):
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            total += estimate_tokens(str(fn.get("arguments") or ""))
    if tools:
        try:
            total += estimate_tokens(_json.dumps(tools))
        except Exception:
            pass
    return total


def _flatten_messages(messages: Optional[List[Dict[str, Any]]]) -> str:
    """Concatenate textual content from chat messages for estimation."""
    if not messages:
        return ""
    parts: List[str] = []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for chunk in content:
                if isinstance(chunk, dict):
                    txt = chunk.get("text")
                    if isinstance(txt, str):
                        parts.append(txt)
    return "\n".join(parts)


def record_llm_usage(*,
                     model: Optional[str],
                     provider: Optional[str] = None,
                     prompt_tokens: int = 0,
                     completion_tokens: int = 0,
                     total_tokens: int = 0,
                     cached_tokens: int = 0,
                     reasoning_tokens: int = 0,
                     usage_details_available: bool = False,
                     duration_ms: int = 0,
                     messages: Optional[List[Dict[str, Any]]] = None,
                     response_text: Optional[str] = None) -> None:
    """Emit a generic ``llm_usage`` event for one successful LLM completion.

    When the provider returns no usage, token counts are estimated from the
    request/response text via tiktoken and the record is flagged ``estimated``.
    Never raises — telemetry must not disturb the LLM path.
    """
    try:
        estimated = False
        if prompt_tokens <= 0 and messages:
            prompt_tokens = estimate_tokens(_flatten_messages(messages))
            estimated = True
        if completion_tokens <= 0 and response_text:
            completion_tokens = estimate_tokens(response_text)
            estimated = True
        if total_tokens <= 0:
            total_tokens = prompt_tokens + completion_tokens

        ctx = _usage_ctx.get() or {}
        record = {
            'source': ctx.get('source') or 'other',
            'agent_id': ctx.get('agent_id'),
            'agent_name': ctx.get('agent_name'),
            'session_id': ctx.get('session_id'),
            'provider': provider or '',
            'model': model or '',
            'prompt_tokens': int(prompt_tokens or 0),
            'completion_tokens': int(completion_tokens or 0),
            'total_tokens': int(total_tokens or 0),
            'cached_tokens': int(cached_tokens or 0),
            'reasoning_tokens': int(reasoning_tokens or 0),
            'usage_details_available': bool(usage_details_available),
            'duration_ms': int(duration_ms or 0),
            'estimated': estimated,
        }

        from backend.event_stream import event_stream
        event_stream.emit('llm_usage', record)
    except Exception:
        _logger.debug("record_llm_usage failed", exc_info=True)
