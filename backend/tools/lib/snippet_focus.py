"""
snippet_focus — Locate dangerous spans in a code snippet and build a focused excerpt.

The safety pipeline knows *which* regex patterns matched a piece of code, but the
approval prompts never showed *where* the danger was. These helpers turn the matched
patterns into:

  * ``compute_highlights(code, matched_patterns)`` — character-offset spans (with the
    line number) of each dangerous match, used by the web UI to highlight in place.
  * ``build_focus_snippet(code, highlights, ...)`` — a bounded, human-readable excerpt
    centered on the dangerous line(s), used by messaging channels (Telegram / Discord /
    WhatsApp) so the dangerous code is always visible instead of being head-truncated.

Both functions are pure (no DB, no I/O) so they are cheap and easy to unit-test.
"""
from __future__ import annotations

import re

MARKER = "» "  # "» " — prefixes dangerous lines
_CONTEXT_PREFIX = "  "  # two spaces — keeps context lines aligned under the marker


def compute_highlights(code: str, matched_patterns: list[dict]) -> list[dict]:
    """Locate each matched pattern inside *code*.

    Args:
        code: The exact snippet the user will see (the tool's ``script``/``code``).
        matched_patterns: Entries from a safety checker's result. Each must carry a
            ``pattern`` regex string; ``category``/``description`` are passed through.

    Returns:
        A start-sorted, de-duplicated list of ``{start, end, line, category,
        description}`` dicts (character offsets into *code*, 1-based ``line``).
        Patterns that fail to compile or no longer match are skipped.
    """
    if not code or not matched_patterns:
        return []

    spans: dict[tuple[int, int], dict] = {}
    for p in matched_patterns:
        pattern = p.get("pattern")
        if not pattern:
            continue
        try:
            m = re.compile(pattern, re.IGNORECASE).search(code)
        except re.error:
            continue
        if m is None:
            continue
        start, end = m.start(), m.end()
        if start == end:  # zero-width match — nothing to highlight
            continue
        key = (start, end)
        if key in spans:
            continue
        spans[key] = {
            "start": start,
            "end": end,
            "line": code.count("\n", 0, start) + 1,
            "category": p.get("category", ""),
            "description": p.get("description", ""),
        }

    return sorted(spans.values(), key=lambda h: h["start"])


def build_focus_snippet(
    code: str,
    highlights: list[dict],
    context_lines: int = 3,
    max_chars: int = 1200,
) -> str:
    """Build a focused excerpt of *code* centered on the dangerous line(s).

    Dangerous lines are prefixed with :data:`MARKER`; surrounding context lines are
    indented to match. Elided regions (before/between/after the shown windows) are
    replaced with a ``… (N lines hidden)`` marker. The result is bounded by
    *max_chars* — context is trimmed first so every dangerous line is always kept.

    Returns plain text suitable for wrapping in a fenced code block. Falls back to a
    plain (bounded) rendering of *code* when there are no highlights.
    """
    if not code:
        return ""

    lines = code.split("\n")
    total = len(lines)

    danger_lines = sorted({h["line"] for h in highlights if 1 <= h.get("line", 0) <= total})
    if not danger_lines:
        # No located danger — fall back to a bounded plain excerpt.
        return _clamp_plain(code, max_chars)

    # Build windows (danger line ± context) and merge overlapping/adjacent ones.
    windows: list[list[int]] = []
    for ln in danger_lines:
        lo = max(1, ln - context_lines)
        hi = min(total, ln + context_lines)
        if windows and lo <= windows[-1][1] + 1:
            windows[-1][1] = max(windows[-1][1], hi)
        else:
            windows.append([lo, hi])

    danger_set = set(danger_lines)
    header = f"# lines {windows[0][0]}–{windows[-1][1]} of {total}"

    def render(win_list: list[list[int]]) -> str:
        out: list[str] = [header]
        prev_hi = 0
        for lo, hi in win_list:
            hidden = lo - prev_hi - 1
            if hidden > 0:
                out.append(f"… ({hidden} line{'s' if hidden != 1 else ''} hidden)")
            for n in range(lo, hi + 1):
                text = lines[n - 1]
                out.append((MARKER if n in danger_set else _CONTEXT_PREFIX) + text)
            prev_hi = hi
        trailing = total - prev_hi
        if trailing > 0:
            out.append(f"… ({trailing} line{'s' if trailing != 1 else ''} hidden)")
        return "\n".join(out)

    snippet = render(windows)

    # Shrink context symmetrically until we fit, never dropping a dangerous line.
    ctx = context_lines
    while len(snippet) > max_chars and ctx > 0:
        ctx -= 1
        windows = []
        for ln in danger_lines:
            lo = max(1, ln - ctx)
            hi = min(total, ln + ctx)
            if windows and lo <= windows[-1][1] + 1:
                windows[-1][1] = max(windows[-1][1], hi)
            else:
                windows.append([lo, hi])
        snippet = render(windows)

    # Still too long (e.g. a single very long dangerous line) — hard clamp as a last resort.
    if len(snippet) > max_chars:
        snippet = snippet[: max_chars - 1].rstrip() + "…"

    return snippet


def _clamp_plain(code: str, max_chars: int) -> str:
    """Bounded plain rendering when no danger could be located."""
    if len(code) <= max_chars:
        return code
    return code[: max_chars - 1].rstrip() + "…"
