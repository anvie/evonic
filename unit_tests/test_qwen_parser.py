"""
Unit tests for evaluator/qwen_parser.py
"""

import json
import pytest
from evaluator.qwen_parser import (
    is_qwen_format,
    extract_qwen_tool_calls,
    qwen_tool_calls_to_openai_format,
    strip_qwen_tool_calls,
)


# ---------------------------------------------------------------------------
# is_qwen_format
# ---------------------------------------------------------------------------

def test_is_qwen_format_positive():
    assert is_qwen_format('<tool_call>\n<function=runpy>\n</function>\n</tool_call>') is True


def test_is_qwen_format_negative():
    assert is_qwen_format('Just a normal response.') is False
    assert is_qwen_format('') is False
    assert is_qwen_format(None) is False


# ---------------------------------------------------------------------------
# extract_qwen_tool_calls — single tool call
# ---------------------------------------------------------------------------

SINGLE_TOOL_CALL = """\
<tool_call>
<function=runpy>
<parameter=code>
import os
result = os.listdir(".")
print(result)
</parameter>
</function>
</tool_call>"""

def test_single_tool_call_name():
    calls = extract_qwen_tool_calls(SINGLE_TOOL_CALL)
    assert calls is not None
    assert len(calls) == 1
    assert calls[0]['name'] == 'runpy'


def test_single_tool_call_arguments():
    calls = extract_qwen_tool_calls(SINGLE_TOOL_CALL)
    assert 'code' in calls[0]['arguments']
    assert 'os.listdir' in calls[0]['arguments']['code']


def test_single_tool_call_multiline_value():
    calls = extract_qwen_tool_calls(SINGLE_TOOL_CALL)
    code = calls[0]['arguments']['code']
    assert '\n' in code  # multiline preserved


# ---------------------------------------------------------------------------
# extract_qwen_tool_calls — multiple parameters
# ---------------------------------------------------------------------------

MULTI_PARAM = """\
<tool_call>
<function=read_file>
<parameter=path>agents/demo_admin/SYSTEM.md</parameter>
<parameter=start_line>1</parameter>
</function>
</tool_call>"""

def test_multiple_params():
    calls = extract_qwen_tool_calls(MULTI_PARAM)
    assert calls is not None
    args = calls[0]['arguments']
    assert args['path'] == 'agents/demo_admin/SYSTEM.md'
    assert args['start_line'] == '1'


# ---------------------------------------------------------------------------
# extract_qwen_tool_calls — multiple tool calls
# ---------------------------------------------------------------------------

MULTI_TOOL = """\
Let me check two things.

<tool_call>
<function=runpy>
<parameter=code>print("hello")</parameter>
</function>
</tool_call>

<tool_call>
<function=read_file>
<parameter=path>README.md</parameter>
</function>
</tool_call>"""

def test_multiple_tool_calls():
    calls = extract_qwen_tool_calls(MULTI_TOOL)
    assert calls is not None
    assert len(calls) == 2
    assert calls[0]['name'] == 'runpy'
    assert calls[1]['name'] == 'read_file'


# ---------------------------------------------------------------------------
# extract_qwen_tool_calls — no tool calls
# ---------------------------------------------------------------------------

def test_no_tool_calls_returns_none():
    assert extract_qwen_tool_calls('Plain text response.') is None
    assert extract_qwen_tool_calls('') is None
    assert extract_qwen_tool_calls(None) is None


# ---------------------------------------------------------------------------
# qwen_tool_calls_to_openai_format
# ---------------------------------------------------------------------------

def test_openai_format_structure():
    calls = extract_qwen_tool_calls(SINGLE_TOOL_CALL)
    openai = qwen_tool_calls_to_openai_format(calls)
    assert len(openai) == 1
    tc = openai[0]
    assert tc['type'] == 'function'
    assert 'id' in tc
    assert tc['function']['name'] == 'runpy'
    # arguments must be valid JSON string
    args = json.loads(tc['function']['arguments'])
    assert 'code' in args


def test_openai_format_empty():
    assert qwen_tool_calls_to_openai_format([]) == []
    assert qwen_tool_calls_to_openai_format(None) == []


def test_openai_format_unique_ids():
    calls = extract_qwen_tool_calls(MULTI_TOOL)
    openai = qwen_tool_calls_to_openai_format(calls)
    ids = [tc['id'] for tc in openai]
    assert len(set(ids)) == len(ids)  # all unique


# ---------------------------------------------------------------------------
# strip_qwen_tool_calls
# ---------------------------------------------------------------------------

def test_strip_removes_tool_blocks():
    text = "Here is the plan.\n\n" + SINGLE_TOOL_CALL + "\n\nDone."
    stripped = strip_qwen_tool_calls(text)
    assert '<tool_call>' not in stripped
    assert 'Here is the plan.' in stripped
    assert 'Done.' in stripped


def test_strip_no_tool_calls_unchanged():
    text = "Plain response with no tool calls."
    assert strip_qwen_tool_calls(text) == text


def test_strip_only_tool_calls_returns_empty():
    stripped = strip_qwen_tool_calls(SINGLE_TOOL_CALL)
    assert stripped == ''


# ---------------------------------------------------------------------------
# Tool calls embedded inside <think> / reasoning/CoT content
# (fallback recovery scenario in agent_runtime.py)
# ---------------------------------------------------------------------------

THINK_WRAPPED_TOOL_CALL = """\
<think>
Oke, aku udah baca semua file yang relevan. Sekarang aku paham situasinya.

Tapi masalahnya, aku perlu cek apakah ada issue lain. Aku cek juga file berikutnya.

<tool_call>
<function=read_file>
<parameter=file_path>
/workspace/skills/kanban/backend/tools/kanban_add_comment.py
</parameter>
</function>
</tool_call>
</think>"""

COT_PROSE_TOOL_CALL = """\
Oke, aku udah baca semua file yang relevan. Sekarang aku paham situasinya.

Tapi masalahnya, aku perlu cek apakah ada issue lain.

<tool_call>
<function=read_file>
<parameter=file_path>/workspace/skills/kanban/backend/tools/kanban_add_comment.py</parameter>
</function>
</tool_call>

More reasoning after the tool call."""


def test_extract_from_think_wrapped_content():
    """Tool calls inside <think> tags should be extractable."""
    # The think tags don't interfere with <tool_call> parsing
    calls = extract_qwen_tool_calls(THINK_WRAPPED_TOOL_CALL)
    assert calls is not None
    assert len(calls) == 1
    assert calls[0]['name'] == 'read_file'
    assert 'kanban_add_comment.py' in calls[0]['arguments']['file_path']


def test_extract_from_cot_prose():
    """Tool calls embedded in plain CoT/reasoning prose should be extractable."""
    calls = extract_qwen_tool_calls(COT_PROSE_TOOL_CALL)
    assert calls is not None
    assert len(calls) == 1
    assert calls[0]['name'] == 'read_file'


def test_openai_format_from_cot_tool_call():
    """Recovered CoT tool calls should convert to valid OpenAI format."""
    calls = extract_qwen_tool_calls(COT_PROSE_TOOL_CALL)
    openai = qwen_tool_calls_to_openai_format(calls)
    assert len(openai) == 1
    tc = openai[0]
    assert tc['type'] == 'function'
    assert tc['function']['name'] == 'read_file'
    args = json.loads(tc['function']['arguments'])
    assert 'file_path' in args


def test_extract_multiple_from_cot():
    """Multiple tool calls inside reasoning text should all be extracted."""
    cot = """\
Let me check two files.

<tool_call>
<function=read_file>
<parameter=file_path>/workspace/a.py</parameter>
</function>
</tool_call>

And also this one:

<tool_call>
<function=read_file>
<parameter=file_path>/workspace/b.py</parameter>
</function>
</tool_call>
"""
    calls = extract_qwen_tool_calls(cot)
    assert calls is not None
    assert len(calls) == 2
    assert calls[0]['arguments']['file_path'] == '/workspace/a.py'
    assert calls[1]['arguments']['file_path'] == '/workspace/b.py'


def test_no_false_positive_from_pure_reasoning():
    """Plain CoT text without tool calls should return None."""
    cot = "Aku udah baca file-nya. Situasinya sudah jelas, tidak perlu tool lagi."
    assert extract_qwen_tool_calls(cot) is None
