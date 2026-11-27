"""Decision capability validation and structured-output provider adapters.

The registry stores this optional model metadata in ``capabilities``::

    {"decision": {"structured_output": true,
                  "schema_primitives": ["choice", "boolean", "score"],
                  "candidate_scores": false, "provider_logits": false}}

Capabilities are additive and never alter a model's primary ``type``.  Values
returned by this module deliberately exclude resolved provider credentials.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

import requests

from backend.tools.lib.decision_models import (
    ContractValidationError, DecisionAdapter, DecisionCancellation, DecisionError,
    DecisionErrorCode, DecisionFieldResult, DecisionInvocation, DecisionResult,
    FieldKind, ProviderMetadata, TimingMetadata, validate_result,
)

SUPPORTED_PRIMITIVES = frozenset(kind.value for kind in FieldKind)

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def is_opencode_endpoint(provider: Any, base_url: Any) -> bool:
    """True for OpenCode Go/Zen gateways, which require x-opencode-session."""
    p = str(provider or "").lower()
    return p.startswith("opencode") or "opencode.ai" in str(base_url or "").lower()



class DecisionCompatibilityError(ValueError):
    """Actionable failure raised before a provider receives a request."""


def _legacy_decision_metadata(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    decision = raw.get("decision")
    return decision if isinstance(decision, Mapping) else {}


def _canonical_operations(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return canonical operations when capabilities use model-capabilities/v1."""
    operations = raw.get("operations")
    return operations if isinstance(operations, Mapping) else {}


def public_decision_capabilities(model: Mapping[str, Any]) -> dict[str, Any]:
    """Return public capability metadata without model connection credentials."""
    raw = model.get("capabilities") or {}
    if not isinstance(raw, Mapping):
        raw = {}
    decision = _legacy_decision_metadata(raw)
    operations = _canonical_operations(raw)
    # Canonical capabilities declare support through the decision/strict_output
    # operations; the legacy decision block remains a supported compatibility shape.
    canonical = operations.get("decision", "unsupported") != "unsupported" and operations.get("strict_output", "unsupported") != "unsupported"
    primitives = decision.get("schema_primitives") or ()
    if not isinstance(primitives, (list, tuple)):
        primitives = ()
    return {
        "model_id": model.get("id"),
        "enabled": bool(model.get("enabled", True)),
        "decision": {
            "structured_output": bool(decision.get("structured_output", False)) or canonical,
            "schema_primitives": sorted(str(value) for value in primitives if str(value) in SUPPORTED_PRIMITIVES),
            "candidate_scores": bool(decision.get("candidate_scores", False)),
            "provider_logits": bool(decision.get("provider_logits", False)),
        },
    }


def validate_decision_compatibility(model: Mapping[str, Any] | None, invocation: DecisionInvocation) -> dict[str, Any]:
    """Validate an assignment or direct test before constructing an adapter."""
    if not model:
        raise DecisionCompatibilityError("No configured model was found for this decision request.")
    capabilities = public_decision_capabilities(model)
    if not capabilities["enabled"]:
        raise DecisionCompatibilityError(f"Model '{model.get('id')}' is disabled and cannot serve decisions.")
    decision = capabilities["decision"]
    if not decision["structured_output"]:
        raise DecisionCompatibilityError(
            f"Model '{model.get('id')}' is not marked with decision.structured_output=true."
        )
    raw = model.get("capabilities") if isinstance(model.get("capabilities"), Mapping) else {}
    schema_versions = raw.get("schema_versions") if isinstance(raw.get("schema_versions"), (list, tuple)) else ()
    if raw.get("version") == "model-capabilities/v1":
        # Canonical capabilities describe structured output by operation and
        # schema version; individual primitives are the reasoner's responsibility.
        if invocation.schema.version not in schema_versions:
            raise DecisionCompatibilityError(
                f"Model '{model.get('id')}' does not declare decision schema version '{invocation.schema.version}'."
            )
    else:
        required = {field.kind.value for field in invocation.schema.fields}
        missing = sorted(required - set(decision["schema_primitives"]))
        if missing:
            raise DecisionCompatibilityError(
                f"Model '{model.get('id')}' does not support decision schema primitives: {', '.join(missing)}."
            )
    adapter_key = model.get("adapter_key")
    if adapter_key:
        if adapter_key not in {"openai.chat_completions.v1", "systemone.questions.v1"}:
            raise DecisionCompatibilityError(
                f"Model '{model.get('id')}' uses unsupported decision adapter '{adapter_key}'."
            )
    elif model.get("api_format", "openai") not in {"openai", "systemone"}:
        raise DecisionCompatibilityError(
            f"Model '{model.get('id')}' uses unsupported decision adapter format '{model.get('api_format')}'."
        )
    return capabilities


def _json_schema(invocation: DecisionInvocation) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in invocation.schema.fields:
        schema: dict[str, Any]
        if field.kind is FieldKind.CHOICE:
            schema = {"type": "string", "enum": list(field.choices)}
        elif field.kind is FieldKind.BOOLEAN:
            schema = {"type": "boolean"}
        else:
            schema = {"type": "number", "minimum": field.minimum, "maximum": field.maximum}
        if field.nullable:
            schema = {"anyOf": [schema, {"type": "null"}]}
        if field.description:
            schema["description"] = field.description
        properties[field.key] = schema
        if field.required:
            required.append(field.key)
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": required}


def _deadline_timeout(cancellation: DecisionCancellation | None, configured: Any) -> float:
    timeout = float(configured or 60)
    if cancellation and cancellation.deadline_unix_ms is not None:
        remaining = (cancellation.deadline_unix_ms - int(time.time() * 1000)) / 1000
        if remaining <= 0:
            raise TimeoutError("Decision deadline elapsed before provider invocation.")
        timeout = min(timeout, remaining)
    return timeout


@dataclass
class OpenAIStructuredDecisionAdapter(DecisionAdapter):
    """First adapter for OpenAI-compatible JSON-schema structured output."""

    model: Mapping[str, Any]
    key: str = "openai-structured-output"

    def decide(self, invocation: DecisionInvocation) -> DecisionResult:
        started = time.monotonic()
        try:
            validate_decision_compatibility(self.model, invocation)
            if invocation.cancellation and invocation.cancellation.cancelled:
                return self._error(invocation, DecisionErrorCode.CANCELLED, "Decision was cancelled before provider invocation.")
            timeout = _deadline_timeout(invocation.cancellation, self.model.get("timeout"))
            base_url = str(self.model.get("base_url") or "").rstrip("/")
            if not base_url:
                return self._error(invocation, DecisionErrorCode.PROVIDER_UNAVAILABLE, "Model has no base_url configured.")
            payload = {
                "model": self.model.get("model_name"),
                "messages": [{"role": "user", "content": json.dumps(invocation.to_dict(), sort_keys=True)}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "decision_result", "strict": True, "schema": _json_schema(invocation)}},
            }
            headers = {"Content-Type": "application/json"}
            if self.model.get("api_key"):
                headers["Authorization"] = f"Bearer {self.model['api_key']}"
            if is_opencode_endpoint(self.model.get("provider"), base_url):
                # OpenCode Go/Zen reject requests without a session id (MissingSessionID).
                headers["x-opencode-session"] = uuid.uuid4().hex
                # Cloudflare in front of OpenCode rejects python-requests user agents (error 1010).
                headers["User-Agent"] = BROWSER_USER_AGENT
            response = requests.post(f"{base_url}/chat/completions", json=payload, headers=headers, timeout=timeout)
            if response.status_code >= 400:
                return self._error(invocation, DecisionErrorCode.PROVIDER_UNAVAILABLE, f"Provider returned HTTP {response.status_code}.", retryable=response.status_code >= 500)
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            values = json.loads(content) if isinstance(content, str) else content
            if not isinstance(values, Mapping):
                raise ValueError("structured output was not an object")
            usage = body.get("usage")
            normalized_usage = None
            if isinstance(usage, Mapping):
                normalized_usage = {
                    key: int(value) for key, value in usage.items()
                    if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                    and isinstance(value, int) and not isinstance(value, bool) and value >= 0
                }
            elapsed_ms = int((time.monotonic() - started) * 1000)
            result = DecisionResult(
                profile=invocation.profile, schema_version=invocation.schema.version,
                fields=tuple(DecisionFieldResult(key, values[key]) for key in values),
                provider=ProviderMetadata(
                    str(self.model.get("provider") or "openai"),
                    str(self.model.get("id") or self.model.get("model_name")),
                    usage=normalized_usage,
                ),
                timing=TimingMetadata(elapsed_ms, elapsed_ms), attempts=1,
            )
            return validate_result(invocation.schema, result)
        except TimeoutError as exc:
            return self._error(invocation, DecisionErrorCode.DEADLINE_EXCEEDED, str(exc))
        except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as exc:
            return self._error(invocation, DecisionErrorCode.PROVIDER_RESPONSE_INVALID, "Provider did not return valid structured decision output.")
        except (DecisionCompatibilityError, ContractValidationError) as exc:
            return self._error(invocation, DecisionErrorCode.NO_COMPATIBLE_MODEL, str(exc))

    def _error(self, invocation: DecisionInvocation, code: DecisionErrorCode, message: str, retryable: bool = False) -> DecisionResult:
        return DecisionResult(invocation.profile, invocation.schema.version, (), None, TimingMetadata(0), 1, error=DecisionError(code, message, retryable=retryable))


def adapter_for_model(model: Mapping[str, Any], invocation: DecisionInvocation) -> DecisionAdapter:
    """Return the supported adapter only after deterministic compatibility checks."""
    validate_decision_compatibility(model, invocation)
    return OpenAIStructuredDecisionAdapter(model)
