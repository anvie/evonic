"""
Model pricing — converts token counts to estimated USD cost.

Prices are USD per 1,000,000 tokens, keyed by a lowercase substring of the
model name. Matching picks the longest key contained in the model name, so
specific keys (e.g. "claude-opus") win over generic ones (e.g. "claude").

Defaults are approximate public list prices and may drift over time — override
per deployment via the plugin's PRICING_JSON variable. Unknown models return
cost None (shown as "unpriced" in the dashboard).
"""

import json
import logging
from typing import Dict, Optional

_logger = logging.getLogger(__name__)

# {substring: {"in": price_per_1M_input, "out": price_per_1M_output}}
DEFAULT_PRICING: Dict[str, Dict[str, float]] = {
    "claude-opus":     {"in": 15.0, "out": 75.0},
    "claude-sonnet":   {"in": 3.0,  "out": 15.0},
    "claude-haiku":    {"in": 0.80, "out": 4.0},
    "claude":          {"in": 3.0,  "out": 15.0},
    "gpt-4o-mini":     {"in": 0.15, "out": 0.60},
    "gpt-4o":          {"in": 2.50, "out": 10.0},
    "gpt-4-turbo":     {"in": 10.0, "out": 30.0},
    "gpt-4":           {"in": 30.0, "out": 60.0},
    "gpt-3.5":         {"in": 0.50, "out": 1.50},
    "o1-mini":         {"in": 1.10, "out": 4.40},
    "o1":              {"in": 15.0, "out": 60.0},
}


def _warn(message: str):
    """Surface a pricing misconfiguration in the log file and in the plugin log panel."""
    _logger.warning("token_monitor pricing: %s", message)
    try:
        from backend.plugin_manager import plugin_manager
        plugin_manager.add_log('token_monitor', 'warn', f"pricing: {message}")
    except Exception:
        pass


def _load_pricing() -> Dict[str, Dict[str, float]]:
    """Merge built-in defaults with the optional PRICING_JSON override.

    A broken override never raises: it is reported through _warn() and the
    built-in defaults stay in effect, so the dashboard keeps rendering.
    """
    pricing = dict(DEFAULT_PRICING)
    try:
        from backend.plugin_manager import plugin_manager
        raw = (plugin_manager.get_plugin_config('token_monitor') or {}).get('PRICING_JSON', '')
    except Exception as e:
        _warn(f"could not read PRICING_JSON ({type(e).__name__}: {e}); using built-in defaults")
        return pricing
    if not raw or not str(raw).strip():
        return pricing
    try:
        override = json.loads(raw)
    except json.JSONDecodeError as e:
        _warn(f"PRICING_JSON is not valid JSON ({e.msg} at line {e.lineno}, "
              f"column {e.colno}); using built-in defaults")
        return pricing
    if not isinstance(override, dict):
        _warn(f"PRICING_JSON must be a JSON object, got {type(override).__name__}; "
              "using built-in defaults")
        return pricing
    for k, v in override.items():
        if not isinstance(v, dict) or 'in' not in v or 'out' not in v:
            _warn(f"ignoring entry {k!r}: expected {{\"in\": <number>, \"out\": <number>}}")
            continue
        try:
            pricing[str(k).lower()] = {'in': float(v['in']), 'out': float(v['out'])}
        except (TypeError, ValueError):
            _warn(f"ignoring entry {k!r}: in/out must be numbers, got {v!r}")
    return pricing


def _match(model: str, pricing: Dict[str, Dict[str, float]]) -> Optional[Dict[str, float]]:
    name = (model or '').lower()
    if not name:
        return None
    best_key = None
    for key in pricing:
        if key in name and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return pricing[best_key] if best_key else None


def cost(model: str, prompt_tokens: int, completion_tokens: int,
         pricing: Optional[Dict[str, Dict[str, float]]] = None) -> Optional[float]:
    """Estimated USD cost for a token count, or None if the model is unpriced."""
    pricing = pricing if pricing is not None else _load_pricing()
    rate = _match(model, pricing)
    if rate is None:
        return None
    return round((prompt_tokens / 1_000_000.0) * rate['in']
                 + (completion_tokens / 1_000_000.0) * rate['out'], 6)
