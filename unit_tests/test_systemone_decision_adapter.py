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


# ── Cloudflare Workers AI Clef: the same questions contract, a few differences ──

import json as _json

from backend.tools.lib.systemone_decision_adapter import clef_model


def _clef_model(name="@cf/cloudflare/clef"):
    return {**_model(), "provider": "cloudflare", "model_name": name,
            "endpoint": "https://api.cloudflare.com/client/v4/accounts/abc/ai/run/@cf/cloudflare/clef"}


def _raw(body, status=200):
    response = MagicMock(status_code=status, is_redirect=False)
    response.iter_content.return_value = [_json.dumps(body).encode()]
    return response


CLEF_ANSWERS = {
    "disposition": {"type": "choice", "choice": "go", "probabilities": {"go": .8, "stop": .2}, "confidence": .6},
    "approved": {"type": "noul", "noul": .85},
    "risk": {"type": "score", "score": 2.0, "legend": {"0": "0", "1": "2.5"}, "probabilities": {"1": .25, "2": .5, "3": .25}, "confidence": .5},
}


def test_a_clef_model_id_is_recognised_in_either_spelling():
    assert [clef_model(n) for n in ("clef", "clef-flash", "@cf/cloudflare/clef-flash", " CLEF ")] == ["clef", "clef-flash", "clef-flash", "clef"]
    assert [clef_model(n) for n in ("", None, "gpt-4", "@cf/meta/llama", "clefs")] == [None] * 5


def test_a_clef_request_holds_only_what_clef_defines():
    payload = lower_questions_request(_invocation(), "@cf/cloudflare/clef-flash")
    assert set(payload) == {"model", "state", "questions"} and payload["model"] == "clef-flash"
    questions = payload["questions"]
    assert questions["disposition"] == {"type": "choice", "instructions": "Choose a disposition.",
                                        "criteria": {"go": "Choose a disposition.", "stop": "Choose a disposition."}}
    assert set(questions["approved"]) == {"type", "instructions"} and questions["approved"]["type"] == "noul"
    assert questions["risk"]["criteria"] == ["0 (lowest)", "2.5", "5", "7.5", "10 (highest)"]
    assert not {"required", "nullable", "minimum", "maximum"} & set(questions["risk"])


def test_a_score_between_whole_bounds_has_a_level_per_step():
    invocation = validate_invocation({
        "profile": {"key": "agent.goal_completion", "version": "1"},
        "schema": {"version": "decision-schema/v1", "fields": [{"key": "risk", "kind": "score", "minimum": 1, "maximum": 4}]}})
    assert lower_questions_request(invocation, "clef")["questions"]["risk"]["criteria"] == ["1 (lowest)", "2", "3", "4 (highest)"]


def test_other_models_still_get_the_systemone_request():
    assert set(lower_questions_request(_invocation(), "something-else")) == {"state", "context", "questions"}
    assert set(lower_questions_request(_invocation())) == {"state", "context", "questions"}


def test_clef_answers_inside_cloudflares_result_wrapper_are_read():
    body = {"result": {"model": "clef", "answers": CLEF_ANSWERS, "usage": {"input_tokens": 812, "output_tokens": 0}},
            "success": True, "errors": [], "messages": []}
    with patch("backend.tools.lib.systemone_decision_adapter.requests.post", return_value=_raw(body)) as post:
        result = SystemOneQuestionsDecisionAdapter(_clef_model()).decide(_invocation())
    assert result.error is None
    sent = _json.loads(post.call_args.kwargs["data"])
    assert sent["model"] == "clef" and "context" not in sent
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer secret"
    fields = {field.key: field for field in result.fields}
    assert fields["disposition"].value == "go" and fields["disposition"].confidence == .6
    assert {c.value: c.probability for c in fields["disposition"].candidates} == {"go": .8, "stop": .2}
    assert fields["approved"].value is True and fields["approved"].confidence == .85
    assert fields["risk"].value == 5.0 and fields["risk"].confidence == .5          # level 2 of 0..4 on a 0..10 scale
    assert result.provider.model_id == "clef" and result.provider.usage == {"input_tokens": 812, "output_tokens": 0}


def test_clef_answers_without_the_wrapper_are_read_too():
    with patch("backend.tools.lib.systemone_decision_adapter.requests.post",
               return_value=_raw({"model": "clef", "answers": CLEF_ANSWERS, "usage": {"input_tokens": 1, "output_tokens": 0}})):
        result = SystemOneQuestionsDecisionAdapter(_clef_model("clef")).decide(_invocation())
    assert result.error is None and sorted(f.key for f in result.fields) == ["approved", "disposition", "risk"]


def test_a_clef_score_level_out_of_range_or_a_failed_wrapper_is_an_invalid_answer():
    bad = {**CLEF_ANSWERS, "risk": {"type": "score", "score": 9, "probabilities": {}, "confidence": .5}}
    for body in ({"model": "clef", "answers": bad}, {"result": {"answers": CLEF_ANSWERS}, "success": False},
                 {"result": {"nothing": True}, "success": True}):
        body = body if "result" in body or "answers" in body else {"answers": bad}
        with patch("backend.tools.lib.systemone_decision_adapter.requests.post", return_value=_raw(body)):
            result = SystemOneQuestionsDecisionAdapter(_clef_model()).decide(_invocation())
        assert result.error.code is DecisionErrorCode.PROVIDER_RESPONSE_INVALID


def test_a_rate_limit_is_retryable_and_a_bad_token_is_not():
    for status, retryable in ((429, True), (503, True), (401, False), (400, False)):
        with patch("backend.tools.lib.systemone_decision_adapter.requests.post", return_value=_raw({}, status)):
            result = SystemOneQuestionsDecisionAdapter(_clef_model()).decide(_invocation())
        assert result.error.code is DecisionErrorCode.PROVIDER_UNAVAILABLE and result.error.retryable is retryable
