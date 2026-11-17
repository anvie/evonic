"""Connectivity probe for native ``systemone.questions.v1`` endpoints.

A SystemOne endpoint has no OpenAI-style ``GET /models`` discovery route, so the
Settings page cannot use the generic discovery probe to test it. This module
answers the only question that matters for those endpoints: does a real,
minimal and deterministic decision request produce a valid decision answer?

The probe reuses the production adapter (``SystemOneQuestionsDecisionAdapter``)
so the dialog exercises the same wire contract the runtime uses, and it returns
plain dictionaries so both the model-level and provider-level test routes can
serve it without a Flask dependency here.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

import requests

PROBE_TIMEOUT_SECONDS = 15.0
PROBE_CONTRACT = "systemone.questions.v1"

# One boolean statement plus one three-way topic classification: deterministic,
# tiny, and free of ``score`` questions (bounded score payloads are rejected by
# some SystemOne deployments).
_DETERMINISTIC_STATE = {
    "statement": "The last invoice charged more than the amount quoted on the pricing page.",
}


def probe_schema():
    from backend.tools.lib.decision_models import DecisionField, DecisionSchema, FieldKind

    return DecisionSchema(
        "decision-schema/v1",
        (
            DecisionField(
                "is_negative",
                FieldKind.BOOLEAN,
                "True when the statement expresses dissatisfaction or a complaint.",
            ),
            DecisionField(
                "category",
                FieldKind.CHOICE,
                "The topic the statement belongs to.",
                choices=("billing", "technical", "other"),
            ),
        ),
    )


def probe_invocation(label: str):
    from backend.tools.lib.decision_models import DecisionInvocation, DecisionProfileRef

    return DecisionInvocation(
        DecisionProfileRef(label, "1"),
        probe_schema(),
        dict(_DETERMINISTIC_STATE),
        policy_metadata={},
        correlation_id=f"connectivity-probe:{label}",
    )


def _diagnostic(endpoint: str, api_key: Optional[str], model_name: Optional[str],
                invocation, timeout: float) -> Dict[str, Any]:
    """One extra POST, used only after a failure, to recover the HTTP status the adapter hides."""
    from backend.tools.lib.systemone_decision_adapter import lower_questions_request

    payload = lower_questions_request(invocation, model_name)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = requests.post(
            endpoint, json=payload, headers=headers, timeout=timeout, allow_redirects=False
        )
    except requests.exceptions.Timeout:
        return {"status_code": None, "timeout": True, "detail": f"no response within {timeout:g}s"}
    except requests.exceptions.RequestException as exc:
        return {"status_code": None, "detail": str(exc)[:300]}
    detail = response.text[:300]
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("code") or detail)[:300]
        elif error:
            detail = str(error)[:300]
        elif body.get("message"):
            detail = str(body["message"])[:300]
    return {"status_code": response.status_code, "detail": detail}


def _failure(endpoint: str, diagnostic: Mapping[str, Any], error_code: Optional[str]) -> Tuple[str, int]:
    """Translate a failed probe into an actionable message instead of a raw provider error."""
    status_code = diagnostic.get("status_code")
    detail = diagnostic.get("detail") or ""
    if diagnostic.get("timeout"):
        return (f"SystemOne model did not answer within {detail} at {endpoint}.", 408)
    if status_code is None:
        return (
            f"The SystemOne service is not listening at {endpoint}. "
            "The endpoint refused the connection or the route is down.",
            502,
        )
    if status_code in (502, 503, 504):
        return (
            f"The SystemOne service is down at {endpoint} (HTTP {status_code}). "
            "The route is reachable but no model is listening on the target port.",
            status_code,
        )
    if status_code == 400:
        return (f"SystemOne rejected the probe payload: {detail}", 400)
    if status_code == 404:
        return (f"The SystemOne endpoint path was not recognized: POST {endpoint} returned HTTP 404.", 404)
    if status_code in (401, 403):
        return (f"SystemOne rejected the credentials (HTTP {status_code}): {detail}", status_code)
    return (
        f"SystemOne probe failed at {endpoint} (HTTP {status_code}, {error_code or 'provider_error'}): {detail}",
        status_code,
    )


def _answer_value(value: Any) -> Any:
    return value


def run_probe(endpoint: str, *, model_name: Optional[str] = None, api_key: Optional[str] = None,
              timeout: Optional[float] = None, label: str = "connectivity") -> Tuple[Dict[str, Any], int]:
    """Probe one endpoint; returns ``(payload, http_status)`` for the Settings dialog."""
    from backend.tools.lib.systemone_decision_adapter import SystemOneQuestionsDecisionAdapter

    if not endpoint:
        return ({"success": False, "error": "No endpoint configured", "status_code": 400}, 400)

    bounded = min(float(timeout or PROBE_TIMEOUT_SECONDS), PROBE_TIMEOUT_SECONDS)
    invocation = probe_invocation(label)
    adapter_model = {
        "id": label,
        "provider": "systemone",
        "model_name": model_name,
        "base_url": endpoint,
        "endpoint": endpoint,
        "api_key": api_key,
        "adapter_key": PROBE_CONTRACT,
        "timeout": bounded,
    }
    result = SystemOneQuestionsDecisionAdapter(adapter_model).decide(invocation)
    if result.error is not None:
        diagnostic = _diagnostic(endpoint, api_key, model_name, invocation, bounded)
        message, status = _failure(endpoint, diagnostic, result.error.code.value)
        return (
            {
                "success": False,
                "error": message,
                "status_code": status,
                "latency_ms": result.timing.total_ms,
                "model": model_name,
                "endpoint": endpoint,
            },
            status if status >= 400 else 502,
        )

    answers = {field.key: _answer_value(field.value) for field in result.fields}
    summary = ", ".join(
        f"{field.key}={'yes' if field.value is True else 'no' if field.value is False else field.value}"
        + (f" ({field.confidence:.2f})" if field.confidence is not None else "")
        for field in result.fields
    )
    return (
        {
            "success": True,
            "message": f"SystemOne decision probe succeeded at {endpoint}",
            "latency_ms": result.timing.total_ms,
            "model": model_name,
            "endpoint": endpoint,
            "answers": answers,
            "probe": {
                "contract": PROBE_CONTRACT,
                "endpoint": endpoint,
                "model": model_name,
                "latency_ms": result.timing.total_ms,
                "input_tokens": (result.provider.usage or {}).get("input_tokens") if result.provider else None,
                "answers": [
                    {
                        "field": field.key,
                        "value": field.value,
                        "confidence": field.confidence,
                        "candidates": [c.value for c in (field.candidates or ())],
                    }
                    for field in result.fields
                ],
                "summary": summary,
            },
        },
        200,
    )
