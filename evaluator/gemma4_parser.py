"""
Gemma 4 Response Parser

Handles Gemma 4's unique output format:
- Thinking: <|channel>thought...<channel|>
- Content: after <channel|> until <turn|> or <|tool_call>
- Tool calls: <|tool_call>call:function_name{params}<tool_call|>
- Special quote token: <|"|> for string values in tool params
"""

import re
import json
from typing import Dict, Any, Optional, List, Tuple


# Gemma 4 format markers
GEMMA4_MARKERS = [
    '<|channel>',
    '<channel|>',
    '<|turn>',
    '<turn|>',
    '<|tool_call>',
    '<tool_call|>',
]


def is_gemma4_format(text: str) -> bool:
    """
    Detect if text is in Gemma 4 format.
    
    Args:
        text: Raw LLM response text
        
    Returns:
        True if Gemma 4 format detected
    """
    if not text:
        return False
    
    # Check for any Gemma 4 markers
    for marker in GEMMA4_MARKERS:
        if marker in text:
            return True
    
    return False


def parse_gemma4_response(text: str) -> Dict[str, Any]:
    """
    Parse Gemma 4 format response into structured components.
    
    Args:
        text: Raw Gemma 4 format response
        
    Returns:
        {
            "thinking": str or None,      # Content from <|channel>thought...<channel|>
            "content": str,               # Main answer content
            "tool_calls": list or None,   # Parsed tool calls
            "raw": str                    # Original text
        }
    """
    result = {
        "thinking": None,
        "content": "",
        "tool_calls": None,
        "raw": text
    }
    
    if not text:
        return result
    
    # Extract thinking content: <|channel>thought...<channel|>
    thinking_pattern = r'<\|channel>thought\s*(.*?)<channel\|>'
    thinking_match = re.search(thinking_pattern, text, re.DOTALL)
    if thinking_match:
        result["thinking"] = thinking_match.group(1).strip()
    
    # Extract tool calls: <|tool_call>call:function{params}<tool_call|>
    tool_calls = extract_gemma4_tool_calls(text)
    if tool_calls:
        result["tool_calls"] = tool_calls
    
    # Extract main content (after <channel|> and before <turn|> or <|tool_call>)
    content = extract_gemma4_content(text)
    result["content"] = content
    
    return result


def extract_gemma4_content(text: str) -> str:
    """
    Extract the main answer content from Gemma 4 response.
    
    Content is located:
    - After <channel|> (end of thinking)
    - Before <turn|> or <|tool_call> or end of string
    
    Args:
        text: Raw Gemma 4 response
        
    Returns:
        Extracted content string
    """
    if not text:
        return ""
    
    # Find the end of thinking block
    channel_end_pos = text.find('<channel|>')
    if channel_end_pos != -1:
        # Start after <channel|>
        start_pos = channel_end_pos + len('<channel|>')
    else:
        # No thinking block, start from beginning
        # But skip any system/user turn markers
        start_pos = 0
        
        # Skip past model turn marker if present
        model_turn_match = re.search(r'<\|turn>model\s*', text)
        if model_turn_match:
            start_pos = model_turn_match.end()
    
    # Find the end boundary
    remaining = text[start_pos:]
    
    # End at <turn|> or <|tool_call> or <eos> or end of string
    end_markers = ['<turn|>', '<|tool_call>', '<eos>', '<|eos|>']
    end_pos = len(remaining)
    
    for marker in end_markers:
        pos = remaining.find(marker)
        if pos != -1 and pos < end_pos:
            end_pos = pos
    
    content = remaining[:end_pos].strip()

    # Strip any residual Gemma 4 markers that leaked into the content
    # (e.g. <|channel> appearing without a matching <channel|> closing tag)
    for marker in GEMMA4_MARKERS:
        content = content.replace(marker, '')
    content = content.strip()

    # Gemma4-12B tokenizer produces "** text**" (space after opening **)
    # for certain tokens, breaking markdown bold rendering.
    # Post-process to collapse the space: "** text**" → "**text**"
    content = re.sub(r'(^|\s)\*\* ', r'\1**', content)

    return content


def extract_gemma4_tool_calls(text: str) -> Optional[List[Dict[str, Any]]]:
    """
    Extract tool calls from Gemma 4 format.
    
    Formats supported:
    1. <|tool_call>function_name{param:<|"|>value<|"|>}<|tool_call|>
    2. <|tool_call>call:function_name{param:<|"|>value<|"|>}<tool_call|>
    3. Multi-line format with newlines
    
    Args:
        text: Raw Gemma 4 response
        
    Returns:
        List of tool call dicts or None if no tool calls
    """
    tool_calls = []
    
    # Pattern 1: <|tool_call>function_name{...}<|tool_call|> (Qwen tool-call format)
    pattern1 = r'<\|tool_call>\s*(\w+)\{([^}]*)\}\s*<\|tool_call\|>'
    
    # Pattern 2: <|tool_call>call:function_name{...}<tool_call|> (original format)
    pattern2 = r'<\|tool_call>call:(\w+)\{([^}]*)\}<tool_call\|>'
    
    # Pattern 3: More relaxed - handles newlines and spacing
    pattern3 = r'<\|tool_call>\s*(?:call:)?(\w+)\s*\{([^}]*)\}\s*<\|?tool_call\|>'
    
    # Try all patterns
    for pattern in [pattern1, pattern2, pattern3]:
        matches = re.findall(pattern, text, re.DOTALL)
        if matches:
            for func_name, params_str in matches:
                # Parse parameters
                params = parse_gemma4_tool_params(params_str)
                
                tool_calls.append({
                    "name": func_name,
                    "arguments": params
                })
            break  # Found matches, stop trying other patterns
    
    return tool_calls if tool_calls else None


def parse_gemma4_tool_params(params_str: str) -> Dict[str, Any]:
    """
    Parse Gemma 4 tool call parameters.
    
    Format: param1:<|"|>value1<|"|>,param2:<|"|>value2<|"|>
    Also handles: param1:value1 (without quotes for non-string values)
    
    Args:
        params_str: Raw parameter string from tool call
        
    Returns:
        Dict of parameter name -> value
    """
    params = {}
    
    if not params_str:
        return params
    
    # Pattern for quoted string values: key:<|"|>value<|"|>
    quoted_pattern = r'(\w+):<\|"\|>([^<]*)<\|"\|>'
    
    # Find all quoted parameters first
    quoted_matches = re.findall(quoted_pattern, params_str)
    for key, value in quoted_matches:
        params[key] = value
    
    # Remove quoted params from string to find unquoted ones
    remaining = re.sub(quoted_pattern, '', params_str)
    
    # Pattern for unquoted values: key:value (numbers, booleans)
    unquoted_pattern = r'(\w+):([^,}\s]+)'
    unquoted_matches = re.findall(unquoted_pattern, remaining)
    
    for key, value in unquoted_matches:
        # Try to parse as number or boolean
        if value.lower() == 'true':
            params[key] = True
        elif value.lower() == 'false':
            params[key] = False
        else:
            try:
                # Try integer first
                params[key] = int(value)
            except ValueError:
                try:
                    # Try float
                    params[key] = float(value)
                except ValueError:
                    # Keep as string
                    params[key] = value
    
    return params


def strip_gemma4_thinking(text: str) -> Tuple[str, Optional[str]]:
    """
    Strip thinking content from Gemma 4 response.
    
    Convenience function that returns (content, thinking) tuple
    similar to standard strip_thinking_tags.
    
    Args:
        text: Raw Gemma 4 response
        
    Returns:
        Tuple of (cleaned_content, thinking_content)
    """
    parsed = parse_gemma4_response(text)
    return parsed["content"], parsed["thinking"]


def gemma4_tool_calls_to_openai_format(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Convert Gemma 4 tool calls to OpenAI-compatible format.
    
    Args:
        tool_calls: List of Gemma 4 parsed tool calls
        
    Returns:
        List in OpenAI tool_calls format
    """
    if not tool_calls:
        return []
    
    openai_format = []
    for i, tc in enumerate(tool_calls):
        openai_format.append({
            "id": f"call_{i}",
            "type": "function",
            "function": {
                "name": tc["name"],
                "arguments": json.dumps(tc["arguments"])
            }
        })
    
    return openai_format
