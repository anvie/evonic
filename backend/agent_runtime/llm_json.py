"""
Robust JSON extraction from LLM responses.

Models routinely wrap JSON in fences, prepend reasoning, or append prose —
naive `text[find('{'):rfind('}')+1]` slices die with "Extra data" whenever
the trailing prose contains a brace. This helper returns the FIRST complete
JSON object via json.JSONDecoder.raw_decode, which tolerates anything after
the object.
"""
from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r'```(?:json)?\s*(.*?)```', re.DOTALL)
_decoder = json.JSONDecoder()


def extract_first_json(text: str, require_key: str = None):
    """Return a complete JSON object (dict) from an LLM response, or None.

    Fenced ```json blocks are preferred; otherwise scans for parseable objects,
    ignoring surrounding prose (reasoning before, commentary after). When
    `require_key` is set, returns the FIRST object that contains that key —
    so a distractor object in the model's prose (e.g. an example, or a nested
    sub-object emitted before the real envelope) is skipped in favor of the
    actual payload. Without it, returns the first parseable object."""
    if not text:
        return None
    candidates = []
    for fence in _FENCE_RE.finditer(text):
        candidates.append(fence.group(1))
    candidates.append(text)
    first_any = None
    for candidate in candidates:
        idx = candidate.find('{')
        while idx != -1:
            try:
                obj, _end = _decoder.raw_decode(candidate[idx:])
                if isinstance(obj, dict):
                    if require_key is None or require_key in obj:
                        return obj
                    if first_any is None:
                        first_any = obj
            except ValueError:
                pass
            idx = candidate.find('{', idx + 1)
    # require_key requested but no object had it — fall back to first object
    return first_any


def complete_truncated_json(text: str):
    """Best-effort parse of a JSON object whose TAIL was cut off
    mid-generation (finish_reason=length: implicit reasoning burned the
    max_tokens budget and the answer stopped mid-object). Closes an
    unterminated string, drops a dangling comma / completes a dangling
    `key:` with null, then closes the open brackets. Returns the dict or
    None — callers that order important fields first recover them even
    when the tail is lost."""
    if not text:
        return None
    idx = text.find('{')
    if idx == -1:
        return None
    s = text[idx:]
    stack = []
    in_str = esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in '{[':
            stack.append(ch)
        elif ch in '}]' and stack:
            stack.pop()
    if not stack:
        return None  # object is closed — the parse failed for another reason
    if in_str:
        s += '"'
    s = s.rstrip()
    if s.endswith(','):
        s = s[:-1].rstrip()
    elif s.endswith(':'):
        s += ' null'
    s += ''.join('}' if opener == '{' else ']' for opener in reversed(stack))
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None
