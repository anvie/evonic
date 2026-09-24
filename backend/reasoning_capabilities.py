"""Static per-model API reasoning capabilities and effort validation."""

import re
from urllib.parse import urlsplit
from typing import List, Optional, TypedDict

from backend.reasoning_effort_error import ReasoningEffortError


class ReasoningCapabilities(TypedDict):
    efforts: List[str]
    default_effort: Optional[str]
    manual: bool

def reasoning_capabilities(efforts=(), default=None, manual=False) -> ReasoningCapabilities:
    """Normalize provider metadata without inventing a universal effort enum."""
    values = list(dict.fromkeys(
        value for value in efforts
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value)
    ))
    return {"efforts": values, "default_effort": default if default in values else None,
            "manual": bool(manual and not values)}

def validate_reasoning_effort(effort, capabilities):
    if effort is None or effort == "":
        return None
    if (not isinstance(effort, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", effort)
            or (not capabilities.get('manual') and effort not in capabilities["efforts"])):
        raise ReasoningEffortError(
            "Reasoning effort is not supported by this provider/model. "
            "Choose a listed effort or select Default (provider)."
        )
    return effort


# API-documented effort levels, reviewed 2026-09-08.
# Codex harness-only Ultra is not an API effort. Keep subscription options conservative;
# do not advertise API defaults as subscription defaults.
# https://developers.openai.com/api/docs/models/gpt-6-astra
# https://developers.openai.com/api/docs/models/gpt-5.6-sol
# https://developers.openai.com/api/docs/models/gpt-5.5
# https://developers.openai.com/api/docs/models/gpt-5.4-mini
REASONING_CATALOG = {
    'codex': {
        'host': 'chatgpt.com', 'api_format': 'codex',
        'models': {
            **{model: reasoning_capabilities(['low', 'medium', 'high', 'xhigh', 'max'])
               for model in ('gpt-6-astra', 'gpt-5.6', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna')},
            **{model: reasoning_capabilities(['low', 'medium', 'high', 'xhigh'])
               for model in ('gpt-5.5', 'gpt-5.4-mini')},
        },
    },
    # https://platform.claude.com/docs/en/build-with-claude/effort
    'anthropic': {
        'host': 'api.anthropic.com', 'api_format': 'anthropic',
        'models': {
            'claude-opus-4-5': reasoning_capabilities(['low', 'medium', 'high'], 'high'),
            **{model: reasoning_capabilities(['low', 'medium', 'high', 'max'], 'high')
               for model in ('claude-opus-4-6', 'claude-sonnet-4-6', 'claude-mythos-preview')},
            **{model: reasoning_capabilities(['low', 'medium', 'high', 'xhigh', 'max'], 'high')
               for model in ('claude-opus-4-7', 'claude-opus-4-8', 'claude-opus-5',
                             'claude-sonnet-5', 'claude-fable-5', 'claude-fable-5-1',
                             'claude-mythos-5', 'claude-mythos-5-1')},
        },
    },
    # https://api-docs.deepseek.com/api/create-chat-completion/
    'deepseek': {
        'host': 'api.deepseek.com', 'api_format': 'openai',
        'models': {model: reasoning_capabilities(['low', 'high', 'max'], 'high')
                   for model in ('deepseek-v4-flash', 'deepseek-v4-pro', 'deepseek-v4-flash-vision-exp')},
    },
}


def reasoning_request_format(config):
    """Choose the wire format without claiming the model supports effort."""
    base_url = config.get('base_url') or ''
    endpoint = urlsplit(base_url)
    if endpoint.scheme not in {'http', 'https'} or not endpoint.hostname:
        return None
    if endpoint.hostname == 'generativelanguage.googleapis.com':
        return None
    api_format = config.get('api_format', 'openai')
    # LLMClient routes ollama.com through its native Ollama payload path.
    if api_format == 'openai' and 'ollama.com' in base_url:
        return None
    return api_format if api_format in {'openai', 'anthropic', 'codex'} else None


def model_reasoning_capabilities(config):
    """Resolve the checked-in catalog only; provider metadata is never consulted."""
    endpoint = urlsplit(config.get('base_url') or '')
    model = config.get('model_name')
    for entry in REASONING_CATALOG.values():
        if (endpoint.scheme == 'https' and endpoint.hostname == entry['host']
                and config.get('api_format', 'openai') == entry['api_format']):
            known = entry['models'].get(model)
            if known is not None:
                return reasoning_capabilities(known['efforts'], known['default_effort'])
    return reasoning_capabilities(manual=bool(reasoning_request_format(config) and model))


def apply_reasoning_effort(payload, config, effort):
    """Merge an already validated override into the existing request payload."""
    if effort is None:
        return
    kind = reasoning_request_format(config)
    if kind == 'codex':
        payload.setdefault('reasoning', {})['effort'] = effort
    elif kind == 'anthropic':
        payload.setdefault('output_config', {})['effort'] = effort
    elif kind == 'openai':
        payload['reasoning_effort'] = effort
    else:
        raise ReasoningEffortError('Reasoning effort is not supported by this provider.')
