import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "static/js/chat-ui.js"
MODULE = ROOT / "static/js/chat-ui/renderers.js"


def _evaluate_summary(path: Path, result: dict) -> str:
    source = path.read_text()
    start = source.index("function _summarizeToolResultValue(value) {")
    end = source.index("function _renderRunpyResult(r) {")
    summary_functions = source[start:end]
    script = f"""{summary_functions}
console.log(summarizeToolResult({json.dumps(result)}));
"""
    return subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_tool_result_summary_renders_validation_reason_code_array():
    result = {
        "accepted": False,
        "reason_code": ["HUMAN_SUBJECT", "SINGLE_PERSON"],
        "user_message": "Foto tidak sesuai standar.",
        "checks": {"subject": "cat"},
    }
    expected = "accepted: false · reason_code: HUMAN_SUBJECT, SINGLE_PERSON · user_message: Foto tidak sesuai standar."

    assert _evaluate_summary(MODULE, result) == expected
    assert _evaluate_summary(BUNDLE, result) == expected


def test_tool_result_summary_limits_scalar_array_output():
    result = {"reason_code": ["ONE", "TWO", "THREE", "FOUR", "FIVE"]}

    assert _evaluate_summary(MODULE, result) == "reason_code: ONE, TWO, THREE, FOUR, …"
