"""The ``reasoning_effort`` contract shared by settings and the LLM runtime.

``reasoning_effort`` is an opt-in hint forwarded to OpenAI-compatible providers
(DeepSeek and friends) in the request payload built by
:mod:`backend.llm_client`. It is stored at two levels:

* ``llm_models.reasoning_effort`` -- the default for every agent using the model;
* ``agents.reasoning_effort``     -- a per-agent override.

``NULL`` means "nothing requested": the payload omits the key entirely and the
provider applies its own default. This module keeps the accepted values and the
precedence rule in one place so the API, the agent factory and the runtime can
never drift apart.
"""

from typing import Any, Optional

#: Canonical values, stored lowercase, in increasing order of effort.
REASONING_EFFORTS = ("low", "medium", "high", "max")


class InvalidReasoningEffort(ValueError):
    """Raised when a value is not one of :data:`REASONING_EFFORTS`."""


def normalize_reasoning_effort(value: Any) -> Optional[str]:
    """Return the canonical stored form of ``value``.

    ``None`` and the empty string mean "inherit" and normalise to ``None``.
    Known values are matched case-insensitively and returned lowercase. Any
    other value raises :class:`InvalidReasoningEffort`, so a caller that
    persists the result can never store an unvalidated string.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidReasoningEffort(
            "reasoning_effort must be one of: " + ", ".join(REASONING_EFFORTS) + "."
        )
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized not in REASONING_EFFORTS:
        raise InvalidReasoningEffort(
            "reasoning_effort must be one of: " + ", ".join(REASONING_EFFORTS) + "."
        )
    return normalized


def resolve_reasoning_effort(
    agent_effort: Optional[str], model_effort: Optional[str]
) -> Optional[str]:
    """Resolve the effort actually sent for a request.

    Precedence: the agent override wins, then the model default, then ``None``
    (the provider default, which means the payload key is omitted).
    """
    return (agent_effort or None) or (model_effort or None)
