"""
filter_pipeline.py — ordered compression pipeline for RTK.

Takes raw command output text and a CompiledFilter, runs through
an ordered sequence of transformations, and returns compressed text.

Usage:
    from token_compressor.filter_pipeline import compress
    result = compress(raw_output, compiled_filter, exit_code=0)

Stages, in order:
    1. strip_ansi              — drop ANSI escape sequences
    2. json_compact            — JSON-only: prune keys/strings/lists, then
                                 re-serialise (short-circuits the rest when it
                                 applies; tool results are one json.dumps line,
                                 so the line-based stages could only corrupt it)
    3. replace                 — per-line regex substitutions
    4. match_output            — whole-output short circuit
    5. strip_lines             — drop matching lines
    6. keep_lines              — keep only matching lines
    7. truncate_lines_at       — cap line length
    8. head_lines / tail_lines — keep head and/or tail
    9. max_lines               — absolute line cap
   10. on_empty                — replacement text for empty output

Critical invariants:
    - exit_code != 0  → output passes through unchanged
    - any exception    → fail-open, return original output
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from .filter_schema import CompiledFilter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ANSI escape sequence regex
# ---------------------------------------------------------------------------

_ANSI_RE: re.Pattern = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compress(
    output: str,
    flt: CompiledFilter,
    exit_code: int = 0,
) -> str:
    """Run the 8-stage compression pipeline on *output* using *flt*.

    Args:
        output: The raw string output from a tool execution.
        flt: A CompiledFilter with pre-compiled regexes and settings.
        exit_code: The exit code of the command.  Non-zero exits are
                   never compressed — the raw output is returned as-is.

    Returns:
        Compressed output string.
    """
    # ------------------------------------------------------------------
    # Guard: never compress error outputs
    # ------------------------------------------------------------------
    if exit_code != 0:
        return output

    # ------------------------------------------------------------------
    # Fail-open: any exception returns the original output
    # ------------------------------------------------------------------
    try:
        return _run_pipeline(output, flt)
    except Exception:
        logger.exception(
            "filter_pipeline.compress: unhandled exception — "
            "returning original output (fail-open)."
        )
        return output


# ---------------------------------------------------------------------------
# Internal pipeline
# ---------------------------------------------------------------------------

def _run_pipeline(text: str, flt: CompiledFilter) -> str:
    """Execute all enabled stages in order."""

    # Stage 1 — strip_ansi
    if flt.strip_ansi:
        text = _stage_strip_ansi(text)

    # Stage 2 — json_compact (single-line JSON tool results)
    # When it applies, the payload has been replaced by a smaller, still valid
    # JSON document, so the line-based stages are skipped: they would mangle it.
    if flt.has_json_stage():
        compacted = _stage_json_compact(text, flt)
        if compacted is not None:
            return compacted

    # Stage 2 — replace (line-by-line substitutions)
    if flt.replace:
        text = _stage_replace(text, flt)

    # Stage 3 — match_output (short-circuit whole-output match)
    if flt.match_output:
        short = _stage_match_output(text, flt)
        if short is not None:
            return short

    # Stage 4 & 5 — strip_lines / keep_lines (mutually exclusive)
    # strip_lines takes priority
    if flt.strip_lines:
        text = _stage_strip_lines(text, flt)
    elif flt.keep_lines:
        text = _stage_keep_lines(text, flt)

    # Stage 6 — truncate_lines_at
    if flt.truncate_lines_at is not None:
        text = _stage_truncate_lines(text, flt)

    # Stage 7 — head_lines / tail_lines
    # Skipped when max_lines is also configured: max_lines subsumes
    # head/tail behaviour with its own head_n / tail_n split.
    if flt.max_lines is None and (
        flt.head_lines is not None or flt.tail_lines is not None
    ):
        text = _stage_head_tail(text, flt)

    # Stage 8 — max_lines (uses filter.head_lines / tail_lines internally)
    if flt.max_lines is not None:
        text = _stage_max_lines(text, flt)

    # Stage 9 — on_empty
    if not text and flt.on_empty is not None:
        text = flt.on_empty

    return text


# ---------------------------------------------------------------------------
# Stage implementations
# ---------------------------------------------------------------------------

def _stage_strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences (colors, cursor movement, etc.)."""
    return _ANSI_RE.sub("", text)


def _stage_json_compact(text: str, flt: CompiledFilter) -> Optional[str]:
    """Prune a JSON payload and re-serialise it compactly.

    Returns the compacted text, or None when the stage does not apply: the
    output is not JSON, a container was expected but a scalar was found, the
    payload is below ``json_compact_above``, or compaction would not shrink it.

    Applying the stage replaces the payload wholesale, so the caller skips the
    line-based stages — they would break the JSON.
    """
    if flt.json_compact_above is not None and len(text) <= flt.json_compact_above:
        return None

    try:
        data: Any = json.loads(text)
    except (TypeError, ValueError):
        return None

    if not isinstance(data, (dict, list)):
        return None

    try:
        pruned = _json_prune(data, flt)
    except Exception:
        logger.warning(
            "filter_pipeline: json compaction failed — keeping original output",
            exc_info=True,
        )
        return None

    result = json.dumps(pruned, ensure_ascii=False)
    if len(result) >= len(text):
        return None
    return result


def _json_prune(node: Any, flt: CompiledFilter) -> Any:
    """Recursively drop keys, cap string lengths and cap list lengths."""
    if isinstance(node, dict):
        drop = set(flt.json_drop_keys)
        return {k: _json_prune(v, flt) for k, v in node.items() if k not in drop}
    if isinstance(node, list):
        items = [_json_prune(v, flt) for v in node]
        limit = flt.json_max_items
        if limit is not None and len(items) > limit:
            hidden = len(items) - limit
            return items[:limit] + [f"...[{hidden} more items]"]
        return items
    if isinstance(node, str):
        limit = flt.json_max_string
        if limit is not None and len(node) > limit:
            return node[: max(0, limit - 1)].rstrip() + "…"
        return node
    return node


def _stage_replace(text: str, flt: CompiledFilter) -> str:
    """Apply regex substitutions from filter.replace[] line-by-line."""
    lines = text.split("\n")
    for pat, replacement in flt.replace:
        lines = [pat.sub(replacement, line) for line in lines]
    return "\n".join(lines)


def _stage_match_output(text: str, flt: CompiledFilter) -> Optional[str]:
    """Check if the entire output matches any match_output pattern.

    Returns the associated message on the first match, or None if
    no pattern matched (meaning the pipeline should continue).
    """
    for pat, message in flt.match_output:
        if pat.search(text):
            return message
    return None


def _stage_strip_lines(text: str, flt: CompiledFilter) -> str:
    """Remove lines matching any pattern from filter.strip_lines[]."""
    lines = text.split("\n")
    kept = [
        line
        for line in lines
        if not any(pat.search(line) for pat in flt.strip_lines)
    ]
    return "\n".join(kept)


def _stage_keep_lines(text: str, flt: CompiledFilter) -> str:
    """Keep only lines matching at least one pattern from filter.keep_lines[]."""
    lines = text.split("\n")
    kept = [
        line
        for line in lines
        if any(pat.search(line) for pat in flt.keep_lines)
    ]
    return "\n".join(kept)


def _stage_truncate_lines(text: str, flt: CompiledFilter) -> str:
    """Cap each line at truncate_lines_at characters, appending '...'."""
    limit = flt.truncate_lines_at
    assert limit is not None
    result: list[str] = []
    for line in text.split("\n"):
        if len(line) > limit:
            result.append(line[:limit] + "...")
        else:
            result.append(line)
    return "\n".join(result)


def _stage_head_tail(text: str, flt: CompiledFilter) -> str:
    """Keep first head_lines and last tail_lines.

    If the total line count is <= head + tail, return all lines.
    Otherwise keep head from top and tail from bottom with a skip
    indicator in between.
    """
    lines = text.split("\n")
    total = len(lines)
    head = flt.head_lines or 0
    tail = flt.tail_lines or 0

    if total <= head + tail:
        return text

    result = lines[:head]
    if tail > 0:
        result.extend(lines[-tail:])
    return "\n".join(result)


def _stage_max_lines(text: str, flt: CompiledFilter) -> str:
    """Absolute line cap.

    If the number of lines exceeds max_lines, keep head_lines from the
    top and tail_lines from the bottom with a skip indicator in between.
    """
    limit = flt.max_lines
    assert limit is not None

    lines = text.split("\n")
    total = len(lines)

    if total <= limit:
        return text

    # Use filter's head_lines / tail_lines for the split, with sensible
    # defaults when not configured.
    head_n: int = flt.head_lines if flt.head_lines is not None else max(1, limit // 2)
    tail_n: int = flt.tail_lines if flt.tail_lines is not None else max(1, limit // 3)

    # Honour explicit zero: head=0 means no head, tail=0 means no tail.
    has_skip = (head_n > 0 and tail_n > 0 and head_n + tail_n < total)
    skip_lines = 1 if has_skip else 0

    # Ensure head + tail + skip <= limit
    if head_n + tail_n + skip_lines > limit:
        if head_n == 0:
            tail_n = limit - skip_lines
        elif tail_n == 0:
            head_n = limit - skip_lines
        else:
            head_n = max(1, (limit - skip_lines) // 2)
            tail_n = limit - head_n - skip_lines
        has_skip = (head_n > 0 and tail_n > 0 and head_n + tail_n < total)

    skipped = total - head_n - tail_n
    head_part = lines[:head_n] if head_n > 0 else []
    tail_part = lines[-tail_n:] if tail_n > 0 else []

    result = list(head_part)
    if skipped > 0 and has_skip:
        result.append(f"...[{skipped} lines skipped]...")
    result.extend(tail_part)

    return "\n".join(result)
