"""OpenCode-only SystemOne routing: URL, headers, TypeSafe payload; other providers unchanged."""
import json
from unittest.mock import MagicMock, patch

from backend.tools.lib.systemone_decision_adapter import SystemOneQuestionsDecisionAdapter, lower_questions_request
from unit_tests.test_systemone_decision_adapter import _invocation

ANSWERS = {
    "disposition": {"type": "choice", "choice": "go", "confidence": 1, "probabilities": {"go": 1, "stop": 0}},
    "approved": {"type": "noul", "noul": 0.65},
    "risk": {"type": "score", "score": 3.0, "confidence": 0.64, "probabilities": {"0": 0.01, "3": 0.77}},
}


def _post(model):
    resp = MagicMock(status_code=200, is_redirect=False)
    resp.iter_content.return_value = [json.dumps({"model": "jev-1.14", "answers": ANSWERS}).encode()]
    with patch("backend.tools.lib.systemone_decision_adapter.requests.post", return_value=resp) as post:
        result = SystemOneQuestionsDecisionAdapter(model).decide(_invocation())
    return result, post.call_args


def test_opencode_url_headers_and_typesafe_payload():
    result, call = _post({"provider": "opencode_go", "base_url": "https://opencode.ai/zen/go/v1",
                          "api_key": "k", "model_name": "jev-1.14"})
    assert call.args[0] == "https://opencode.ai/zen/go/v1/systemone"
    h = call.kwargs["headers"]
    assert h["x-opencode-session"] and h["User-Agent"].startswith("Mozilla/5.0")
    body = json.loads(call.kwargs["data"])
    assert body["model"] == "jev-1.14" and "context" not in body
    assert body["questions"]["approved"] == {"type": "noul", "instructions": body["questions"]["approved"]["instructions"]}
    assert isinstance(body["questions"]["risk"]["criteria"], list)
    assert result.error is None
    fields = {f.key: f for f in result.fields}
    assert fields["disposition"].value == "go" and fields["approved"].value is True


def test_non_opencode_unchanged():
    _, call = _post({"provider": "systemone", "endpoint": "https://example.invalid/questions", "model_name": "LiquidAI/d1-3B"})
    assert call.args[0] == "https://example.invalid/questions"
    assert "x-opencode-session" not in call.kwargs["headers"] and "User-Agent" not in call.kwargs["headers"]
    assert lower_questions_request(_invocation(), "LiquidAI/d1-3B") == json.loads(call.kwargs["data"])
    assert "context" in json.loads(call.kwargs["data"])
