"""Native ``systemone.questions.v1`` Decision Models adapter.

SystemOne deliberately uses its questions wire contract; it is never lowered to
an OpenAI chat-completions request.  This module is the only generic decision
consumer that knows that private protocol.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Mapping

import requests

from backend.tools.lib.decision_models import (
    ContractValidationError, DecisionCandidate, DecisionError, DecisionErrorCode,
    DecisionField, DecisionFieldResult, DecisionInvocation, DecisionResult,
    FieldKind, ProviderMetadata, TimingMetadata, validate_result,
)

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 64 * 1024


def _error(invocation: DecisionInvocation, code: DecisionErrorCode, message: str,
           *, retryable: bool = False) -> DecisionResult:
    return DecisionResult(invocation.profile, invocation.schema.version, (), None,
                          TimingMetadata(0), 1, error=DecisionError(code, message, retryable))


# SystemOne's native question types are choice, score and ``noul``: a true/false statement that is
# answered with the probability (0..1) that it is true. Evonic's ``boolean`` field kind maps to it.
_NATIVE_TYPE = {FieldKind.CHOICE: "choice", FieldKind.BOOLEAN: "noul", FieldKind.SCORE: "score"}


def _question(field: DecisionField) -> dict[str, Any]:
    question: dict[str, Any] = {
        "type": _NATIVE_TYPE[field.kind],
        "instructions": field.description or f"Return the {field.key} decision field.",
        "required": field.required,
        "nullable": field.nullable,
    }
    if field.default is not None:
        question["default"] = field.default
    if field.kind is FieldKind.CHOICE:
        question["criteria"] = {choice: field.description or choice for choice in field.choices}
    elif field.kind is FieldKind.SCORE:
        question["minimum"] = field.minimum
        question["maximum"] = field.maximum
    return question


def lower_questions_request(invocation: DecisionInvocation) -> dict[str, Any]:
    """Lower a generic invocation into the native, versioned SystemOne shape."""
    return {
        "state": json.dumps(invocation.input_context, sort_keys=True, separators=(",", ":")),
        "context": {
            "profile_key": invocation.profile.key,
            "profile_version": invocation.profile.version,
            "schema_version": invocation.schema.version,
            "correlation_id": invocation.correlation_id,
            "policy_metadata": dict(invocation.policy_metadata),
        },
        "questions": {field.key: _question(field) for field in invocation.schema.fields},
    }


def _unit(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if 0.0 <= value <= 1.0 else None


def _candidates(raw: Any) -> tuple[DecisionCandidate, ...]:
    candidates: list[DecisionCandidate] = []
    if isinstance(raw, Mapping):
        raw = [{"value": value, "probability": probability} for value, probability in raw.items()]
    if not isinstance(raw, list):
        return ()
    for candidate in raw:
        if not isinstance(candidate, Mapping) or "value" not in candidate:
            continue
        probability = _unit(candidate.get("probability"))
        score = candidate.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            score = None
        candidates.append(DecisionCandidate(candidate["value"], score, probability))
    return tuple(candidates)


def _answer_value(answer: Mapping[str, Any], field: DecisionField) -> Any:
    if "value" in answer:
        return answer["value"]
    key = {FieldKind.CHOICE: "choice", FieldKind.BOOLEAN: "boolean", FieldKind.SCORE: "score"}[field.kind]
    return answer.get(key)


def _noul_probability(answer: Mapping[str, Any], field: DecisionField) -> float | None:
    """The probability that a boolean statement is true, when the provider answered natively."""
    if field.kind is not FieldKind.BOOLEAN or "value" in answer or "boolean" in answer or "noul" not in answer:
        return None
    probability = _unit(answer.get("noul"))
    if probability is None:
        raise ValueError("noul out of range")
    return probability


@dataclass
class SystemOneQuestionsDecisionAdapter:
    """Bounded native SystemOne decision client with normalized provenance."""

    model: Mapping[str, Any]
    key: str = "systemone.questions.v1"

    def decide(self, invocation: DecisionInvocation) -> DecisionResult:
        started = time.monotonic()
        if invocation.cancellation and invocation.cancellation.cancelled:
            return _error(invocation, DecisionErrorCode.CANCELLED, "Decision was cancelled before provider invocation.")
        endpoint = str(self.model.get("endpoint") or self.model.get("base_url") or "").rstrip("/")
        if not endpoint:
            return _error(invocation, DecisionErrorCode.PROVIDER_UNAVAILABLE, "SystemOne provider is not configured.")
        payload = lower_questions_request(invocation)
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            return _error(invocation, DecisionErrorCode.INVALID_REQUEST, "SystemOne request exceeds the payload limit.")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.model.get("api_key"):
            headers["Authorization"] = f"Bearer {self.model['api_key']}"
        try:
            response = requests.post(endpoint, data=encoded, headers=headers,
                                     timeout=float(self.model.get("timeout") or 60), allow_redirects=False, stream=True)
            if response.is_redirect:
                return _error(invocation, DecisionErrorCode.PROVIDER_UNAVAILABLE, "SystemOne redirected the request.")
            if response.status_code >= 400:
                return _error(invocation, DecisionErrorCode.PROVIDER_UNAVAILABLE,
                              "SystemOne provider is unavailable.", retryable=response.status_code >= 500)
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(8192):
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    return _error(invocation, DecisionErrorCode.PROVIDER_RESPONSE_INVALID, "SystemOne response exceeds the response limit.")
                chunks.append(chunk)
            raw = b"".join(chunks)
            body = json.loads(raw)
            answers = body.get("answers") if isinstance(body, Mapping) else None
            if not isinstance(answers, Mapping):
                raise ValueError("answers missing")
            fields: list[DecisionFieldResult] = []
            for field in invocation.schema.fields:
                answer = answers.get(field.key)
                if answer is None and not field.required:
                    continue
                if not isinstance(answer, Mapping):
                    raise ValueError("answer missing")
                confidence = _unit(answer.get("confidence"))
                candidates = _candidates(answer.get("candidates", answer.get("probabilities")))
                probability = _noul_probability(answer, field)
                if probability is not None:
                    # P(true) decides the value; the confidence is how far it is from a coin flip's other side.
                    selected = probability >= 0.5
                    candidates = (DecisionCandidate(True, None, probability),
                                  DecisionCandidate(False, None, 1.0 - probability))
                    confidence = probability if selected else 1.0 - probability
                else:
                    selected = _answer_value(answer, field)
                    if confidence is None:
                        confidence = next((item.probability for item in candidates if item.value == selected and item.probability is not None), None)
                fields.append(DecisionFieldResult(field.key, selected, confidence, candidates))
            model_id = body.get("model_id") if isinstance(body.get("model_id"), str) else None
            elapsed_ms = int((time.monotonic() - started) * 1000)
            result = DecisionResult(invocation.profile, invocation.schema.version, tuple(fields),
                                    ProviderMetadata(str(self.model.get("provider") or "systemone"), model_id or str(self.model.get("id") or "systemone")),
                                    TimingMetadata(elapsed_ms, elapsed_ms), 1)
            return validate_result(invocation.schema, result)
        except requests.Timeout:
            return _error(invocation, DecisionErrorCode.DEADLINE_EXCEEDED, "SystemOne request timed out.", retryable=True)
        except requests.RequestException:
            return _error(invocation, DecisionErrorCode.PROVIDER_UNAVAILABLE, "SystemOne provider is unavailable.", retryable=True)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, ContractValidationError):
            return _error(invocation, DecisionErrorCode.PROVIDER_RESPONSE_INVALID, "SystemOne returned invalid decision output.")
