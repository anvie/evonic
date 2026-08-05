"""Tests for snippet_focus (dangerous-code highlight + focused excerpt)."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.tools.lib.snippet_focus import (
    MARKER,
    compute_highlights,
    build_focus_snippet,
)


# --- compute_highlights -----------------------------------------------------

def test_compute_highlights_locates_match():
    code = "echo hi\nrm -rf /data\necho done"
    matched = [{"pattern": r"rm\s+-rf", "category": "file_destruction", "description": "rm -rf"}]
    hl = compute_highlights(code, matched)
    assert len(hl) == 1
    h = hl[0]
    assert code[h["start"]:h["end"]] == "rm -rf"
    assert h["line"] == 2
    assert h["category"] == "file_destruction"


def test_compute_highlights_empty_when_no_match():
    assert compute_highlights("echo safe", [{"pattern": r"rm\s+-rf"}]) == []


def test_compute_highlights_handles_bad_regex_and_empty():
    assert compute_highlights("", [{"pattern": r"x"}]) == []
    assert compute_highlights("code", []) == []
    # An un-compilable regex is skipped, not raised.
    assert compute_highlights("code", [{"pattern": r"("}]) == []


def test_compute_highlights_sorted_and_deduped():
    code = "sudo rm -rf /a"
    matched = [
        {"pattern": r"rm\s+-rf", "category": "file_destruction"},
        {"pattern": r"sudo", "category": "privilege_escalation"},
        {"pattern": r"sudo", "category": "privilege_escalation"},  # duplicate span
    ]
    hl = compute_highlights(code, matched)
    starts = [h["start"] for h in hl]
    assert starts == sorted(starts)
    assert starts == [0, 5]  # sudo first, then rm -rf; dedup removed the repeat


# --- build_focus_snippet ----------------------------------------------------

def test_focus_snippet_keeps_danger_on_last_line_of_long_script():
    lines = [f"echo step {i}" for i in range(60)]
    lines.append("rm -rf /data")  # dangerous line is the last one (line 61)
    code = "\n".join(lines)
    hl = compute_highlights(code, [{"pattern": r"rm\s+-rf", "category": "file_destruction"}])

    snippet = build_focus_snippet(code, hl, context_lines=3, max_chars=1200)

    assert "rm -rf /data" in snippet          # the danger is present...
    assert MARKER + "rm -rf /data" in snippet  # ...and marked
    assert "hidden" in snippet                 # the head was elided, not the danger
    assert len(snippet) <= 1200
    # The 500-char head-truncation bug: naive code[:500] would have dropped this line.
    assert "rm -rf /data" not in code[:500]


def test_focus_snippet_marks_only_danger_lines():
    code = "a = 1\nrm -rf /x\nb = 2"
    hl = compute_highlights(code, [{"pattern": r"rm\s+-rf"}])
    snippet = build_focus_snippet(code, hl, context_lines=1)
    out_lines = snippet.split("\n")
    marked = [ln for ln in out_lines if ln.startswith(MARKER)]
    assert marked == [MARKER + "rm -rf /x"]


def test_focus_snippet_fallback_when_no_highlights():
    code = "echo one\necho two"
    # No highlights → bounded plain rendering (returns the code unchanged when short).
    assert build_focus_snippet(code, []) == code


def test_focus_snippet_respects_max_chars():
    code = "\n".join(f"line {i} " + ("x" * 40) for i in range(200))
    # Put the danger in the middle.
    code = code + "\nrm -rf /data"
    hl = compute_highlights(code, [{"pattern": r"rm\s+-rf"}])
    snippet = build_focus_snippet(code, hl, context_lines=3, max_chars=300)
    assert len(snippet) <= 300
    assert "rm -rf /data" in snippet


# --- pipeline integration ---------------------------------------------------

def test_pipeline_attaches_highlights_and_focus_snippet():
    from backend.tools.lib.safety_pipeline import get_safety_pipeline

    # A script whose dangerous command is far down a long body.
    body = "\n".join(f"echo step {i}" for i in range(40))
    code = body + "\nrm -rf /data"
    result = get_safety_pipeline().check(code, tool_type='bash')

    assert result["level"] == "requires_approval"
    info = result["approval_info"]
    assert info is not None
    assert info.get("highlights"), "expected non-empty highlights"
    assert "rm -rf" in info.get("focus_snippet", "")
    # Highlight offsets must map onto the exact code the frontend renders.
    h = info["highlights"][0]
    assert "rm" in code[h["start"]:h["end"]]
