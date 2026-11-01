"""Conformance tests for the native SystemOne questions decision adapter."""
from unittest.mock import MagicMock, patch

from backend.tools.lib.decision_models import DecisionErrorCode, validate_invocation
from backend.tools.lib.systemone_decision_adapter import (
    MAX_RESPONSE_BYTES,
    SystemOneQuestionsDecisionAdapter,
    lower_questions_request,
)


def _invocation():
    return validate_invocation({
        "profile": {"key": "agent.goal_completion", "version": "1"},
        "schema": {"version": "decision-schema/v1", "fields": [
            {"key": "disposition", "kind": "choice", "description": "Choose a disposition.", "choices": ["go", "stop"]},
            {"key": "approved", "kind": "boolean"},
            {"key": "risk", "kind": "score", "minimum": 0, "maximum": 10},
            {"key": "note", "kind": "choice", "choices": ["a", "b"], "required": False, "nullable": True},
        ]},
        "input_context": {"subject": "bounded test"},
        "correlation_id": "correlation-1",
    })


def _model():
    return {"id": "systemone/model", "provider": "systemone", "endpoint": "https://example.invalid/questions", "api_key": "secret", "timeout": 3, "adapter_key": "systemone.questions.v1"}


def _response(answers):
    response = MagicMock(status_code=200, is_redirect=False)
    response.iter_content.return_value = [__import__("json").dumps({"answers": answers, "model_id": "systemone-v1"}).encode()]
    return response


def test_lowering_uses_native_questions_contract_for_arbitrary_schema():
    payload = lower_questions_request(_invocation())
    assert set(payload) == {"state", "context", "questions"}
    assert "messages" not in payload and "response_format" not in payload
    assert payload["questions"]["disposition"]["criteria"] == {"go": "Choose a disposition.", "stop": "Choose a disposition."}
    # SystemOne's native boolean question type is "noul" (answered with P(true)).
    assert payload["questions"]["approved"]["type"] == "noul"
    assert payload["questions"]["risk"]["minimum"] == 0.0
    assert payload["questions"]["note"]["required"] is False
    assert payload["questions"]["note"]["nullable"] is True


def test_adapter_normalizes_values_and_candidate_probability_provenance():
    answers = {
        "disposition": {"choice": "go", "candidates": [{"value": "go", "probability": .91}, {"value": "stop", "probability": .09}]},
        "approved": {"boolean": True, "confidence": .8},
        "risk": {"score": 7.5, "confidence": .7},
    }
    with patch("backend.tools.lib.systemone_decision_adapter.requests.post", return_value=_response(answers)) as post:
        result = SystemOneQuestionsDecisionAdapter(_model()).decide(_invocation())
    assert result.error is None
    fields = {field.key: field for field in result.fields}
    assert fields["disposition"].value == "go"
    assert fields["disposition"].confidence == .91
    assert fields["disposition"].candidates[0].probability == .91
    assert fields["approved"].value is True and fields["risk"].value == 7.5
    assert "messages" not in str(post.call_args.kwargs)
    assert post.call_args.args[0] == "https://example.invalid/questions"


def test_adapter_rejects_oversized_and_invalid_responses_without_echoing_content():
    response = MagicMock(status_code=200, is_redirect=False)
    response.iter_content.return_value = [b"x" * (MAX_RESPONSE_BYTES + 1)]
    with patch("backend.tools.lib.systemone_decision_adapter.requests.post", return_value=response):
        result = SystemOneQuestionsDecisionAdapter(_model()).decide(_invocation())
    assert result.error.code is DecisionErrorCode.PROVIDER_RESPONSE_INVALID
    assert "x" * 20 not in result.error.message


def test_cancelled_request_is_not_transmitted():
    invocation = validate_invocation({
        "profile": {"key": "agent.goal_completion", "version": "1"},
        "schema": {"version": "decision-schema/v1", "fields": [{"key": "allowed", "kind": "boolean"}]},
        "cancellation": {"cancelled": True},
    })
    with patch("backend.tools.lib.systemone_decision_adapter.requests.post") as post:
        result = SystemOneQuestionsDecisionAdapter(_model()).decide(invocation)
    assert result.error.code is DecisionErrorCode.CANCELLED
    post.assert_not_called()


def _decide(answers):
    with patch("backend.tools.lib.systemone_decision_adapter.requests.post", return_value=_response(answers)):
        return SystemOneQuestionsDecisionAdapter(_model()).decide(_invocation())


def _base_answers(**override):
    answers = {"disposition": {"choice": "go"}, "approved": {"noul": 0.5}, "risk": {"score": 1}}
    answers.update(override)
    return answers


def test_native_boolean_answer_is_the_probability_that_the_statement_is_true():
    high = {f.key: f for f in _decide(_base_answers(approved={"type": "noul", "noul": 0.908})).fields}["approved"]
    assert high.value is True and abs(high.confidence - 0.908) < 1e-9
    assert [(c.value, round(c.probability, 3)) for c in high.candidates] == [(True, 0.908), (False, 0.092)]
    low = {f.key: f for f in _decide(_base_answers(approved={"noul": 0.036})).fields}["approved"]
    assert low.value is False and abs(low.confidence - 0.964) < 1e-9
    assert {f.key: f for f in _decide(_base_answers(approved={"noul": 0.5})).fields}["approved"].value is True


def test_native_boolean_answers_outside_zero_to_one_are_invalid_output():
    for bad in (1.4, -0.1, "yes", True):
        result = _decide(_base_answers(approved={"noul": bad}))
        assert result.error is not None and result.error.code is DecisionErrorCode.PROVIDER_RESPONSE_INVALID, bad


def test_explicit_boolean_values_are_still_accepted():
    approved = {f.key: f for f in _decide(_base_answers(approved={"value": False, "confidence": .8})).fields}["approved"]
    assert approved.value is False and approved.confidence == .8
