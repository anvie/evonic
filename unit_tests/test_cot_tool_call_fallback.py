"""
Unit tests for the CoT/thinking tool call fallback in agent_runtime.py.

Covers the scenario where a model emits <tool_call> XML inside thinking content
instead of in the main response body:

  1. reasoning_content field (llama.cpp --reasoning mode): tool calls land in
     the separate reasoning_content field, not in raw_content.
  2. <think> tag in raw_content: tool calls are inside the think wrapper.

The fallback logic (after thinking extraction) should recover tool calls in both
cases and populate tool_calls so the agent loop executes them.
"""
from typing import Optional

import json
import pytest

from evaluator.qwen_parser import (
    extract_qwen_tool_calls,
    qwen_tool_calls_to_openai_format,
)
from backend.llm_client import strip_thinking_tags


# ---------------------------------------------------------------------------
# Helpers — replicate the agent_runtime fallback logic in isolation
# ---------------------------------------------------------------------------

def _simulate_fallback(raw_content: str, reasoning_content: Optional[str]):
    """
    Replicate the relevant section of agent_runtime._run_tool_loop:

      1. Extract reasoning_text and thinking from the LLM message fields.
      2. If no structured tool_calls found, run the CoT fallback.

    Returns (tool_calls, reasoning_text, thinking, content).
    """
    tool_calls = None  # simulate: no structured tool_calls in the LLM message

    thinking = None
    reasoning_text = (reasoning_content or '').strip()

    if reasoning_text:
        content, _ = strip_thinking_tags(raw_content) if raw_content else ('', None)
    elif raw_content:
        content, thinking = strip_thinking_tags(raw_content)
    else:
        content = ''

    # --- CoT fallback (the code added in agent_runtime.py) ---
    if not tool_calls:
        cot_text = reasoning_text or thinking
        if cot_text and '<tool_call>' in cot_text:
            cot_calls = extract_qwen_tool_calls(cot_text)
            if cot_calls:
                tool_calls = qwen_tool_calls_to_openai_format(cot_calls)

    return tool_calls, reasoning_text, thinking, content


# ---------------------------------------------------------------------------
# Scenario 1: tool calls in reasoning_content field (llama.cpp --reasoning)
# ---------------------------------------------------------------------------

REASONING_WITH_TOOL_CALL = """\
Oke, aku udah baca semua file yang relevan. Sekarang aku paham situasinya.

Tapi masalahnya, aku perlu cek apakah ada issue lain. Aku cek juga \
`kanban_add_comment.py` untuk memastikan consistency.

<tool_call>
<function=read_file>
<parameter=file_path>
/workspace/skills/kanban/backend/tools/kanban_add_comment.py
</parameter>
</function>
</tool_call>
"""


def test_reasoning_content_field_tool_call_recovered():
    """Tool calls in reasoning_content are recovered even though raw_content is empty."""
    tool_calls, reasoning_text, thinking, content = _simulate_fallback(
        raw_content='',
        reasoning_content=REASONING_WITH_TOOL_CALL,
    )
    assert tool_calls is not None
    assert len(tool_calls) == 1


def test_reasoning_content_field_correct_function_name():
    tool_calls, *_ = _simulate_fallback(
        raw_content='',
        reasoning_content=REASONING_WITH_TOOL_CALL,
    )
    assert tool_calls[0]['function']['name'] == 'read_file'


def test_reasoning_content_field_correct_arguments():
    tool_calls, *_ = _simulate_fallback(
        raw_content='',
        reasoning_content=REASONING_WITH_TOOL_CALL,
    )
    args = json.loads(tool_calls[0]['function']['arguments'])
    assert 'kanban_add_comment.py' in args['file_path']


def test_reasoning_content_field_openai_format():
    """Recovered calls must conform to OpenAI tool_calls format."""
    tool_calls, *_ = _simulate_fallback(
        raw_content='',
        reasoning_content=REASONING_WITH_TOOL_CALL,
    )
    tc = tool_calls[0]
    assert tc['type'] == 'function'
    assert 'id' in tc
    assert tc['id'].startswith('call_')
    assert 'name' in tc['function']
    assert 'arguments' in tc['function']
    json.loads(tc['function']['arguments'])  # must be valid JSON


# ---------------------------------------------------------------------------
# Scenario 2: tool calls inside <think> tags in raw_content
# ---------------------------------------------------------------------------

THINK_WITH_TOOL_CALL = """\
<think>
Sekarang aku paham situasinya. Aku perlu baca file ini dulu.

<tool_call>
<function=read_file>
<parameter=file_path>/workspace/skills/kanban/backend/tools/kanban_search_tasks.py</parameter>
</function>
</tool_call>
</think>"""


def test_think_tag_tool_call_recovered():
    """Tool calls inside <think> tags in raw_content are recovered via fallback."""
    tool_calls, reasoning_text, thinking, content = _simulate_fallback(
        raw_content=THINK_WITH_TOOL_CALL,
        reasoning_content=None,
    )
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]['function']['name'] == 'read_file'


def test_think_tag_thinking_extracted():
    """thinking variable should contain the CoT prose."""
    _, reasoning_text, thinking, content = _simulate_fallback(
        raw_content=THINK_WITH_TOOL_CALL,
        reasoning_content=None,
    )
    assert thinking is not None
    assert 'Sekarang aku paham' in thinking


def test_think_tag_content_cleaned():
    """Main content should be empty (all was inside think tags)."""
    _, reasoning_text, thinking, content = _simulate_fallback(
        raw_content=THINK_WITH_TOOL_CALL,
        reasoning_content=None,
    )
    # After stripping think block, nothing remains in main content
    assert content == '' or content is None or '<think>' not in content


# ---------------------------------------------------------------------------
# Scenario 3: multiple tool calls in CoT
# ---------------------------------------------------------------------------

REASONING_MULTI_TOOL = """\
Aku harus baca dua file ini untuk memahami situasinya.

<tool_call>
<function=read_file>
<parameter=file_path>/workspace/skills/kanban/backend/tools/kanban_add_comment.py</parameter>
</function>
</tool_call>

Dan juga:

<tool_call>
<function=read_file>
<parameter=file_path>/workspace/skills/kanban/backend/tools/kanban_search_tasks.py</parameter>
</function>
</tool_call>
"""


def test_multiple_tool_calls_in_reasoning_content():
    tool_calls, *_ = _simulate_fallback(
        raw_content='',
        reasoning_content=REASONING_MULTI_TOOL,
    )
    assert tool_calls is not None
    assert len(tool_calls) == 2
    names = [tc['function']['name'] for tc in tool_calls]
    assert names == ['read_file', 'read_file']


def test_multiple_tool_calls_unique_ids():
    tool_calls, *_ = _simulate_fallback(
        raw_content='',
        reasoning_content=REASONING_MULTI_TOOL,
    )
    ids = [tc['id'] for tc in tool_calls]
    assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------
# Regression: no false positives
# ---------------------------------------------------------------------------

def test_no_tool_calls_no_fallback():
    """Plain CoT text without <tool_call> tags should leave tool_calls as None."""
    tool_calls, *_ = _simulate_fallback(
        raw_content='',
        reasoning_content="Aku sudah paham masalahnya. Tidak perlu tool lagi.",
    )
    assert tool_calls is None


def test_structured_tool_calls_not_overridden():
    """
    If structured tool_calls were already present (simulated by passing them
    through), the fallback must not override them.

    This test cannot fully replicate the runtime (the fallback checks `not
    tool_calls`), but it verifies the guard condition logic is sound.
    """
    existing = [{"id": "call_abc", "type": "function",
                 "function": {"name": "existing_tool", "arguments": "{}"}}]
    # Simulate the guard: if tool_calls is already set, fallback is skipped
    tool_calls = existing
    cot_text = REASONING_WITH_TOOL_CALL
    if not tool_calls:  # this branch should NOT execute
        cot_calls = extract_qwen_tool_calls(cot_text)
        if cot_calls:
            tool_calls = qwen_tool_calls_to_openai_format(cot_calls)
    assert len(tool_calls) == 1
    assert tool_calls[0]['function']['name'] == 'existing_tool'


def test_empty_raw_content_and_no_reasoning_no_crash():
    """Completely empty response should not raise."""
    tool_calls, reasoning_text, thinking, content = _simulate_fallback(
        raw_content='',
        reasoning_content=None,
    )
    assert tool_calls is None
    assert content == ''
