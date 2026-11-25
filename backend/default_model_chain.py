"""Global model fallback chains (default model and vision model).

Each chain has a main model and up to ``MAX_DEFAULT_FALLBACKS`` fallback
models, tried in order when the main one fails. The fallbacks are stored as a
JSON list under the chain key. The older single-model settings (the legacy
keys) are kept in sync with the first entries so older readers still work,
and they are read as the chain on installs that predate the list.
"""

import json
from typing import Any, Dict, List, Sequence

MAX_DEFAULT_FALLBACKS = 5


def _clean(ids, limit: int = MAX_DEFAULT_FALLBACKS) -> List[str]:
    """Drop blanks and duplicates, keep order, cap at the maximum."""
    out: List[str] = []
    for model_id in ids or []:
        model_id = str(model_id or '').strip()
        if model_id and model_id not in out:
            out.append(model_id)
    return out[:limit]


class FallbackChain:
    """A fallback list setting plus the legacy per-slot keys it mirrors."""

    def __init__(self, chain_key: str, legacy_keys: Sequence[str],
                 limit: int = MAX_DEFAULT_FALLBACKS):
        self.chain_key = chain_key
        self.legacy_keys = tuple(legacy_keys)
        self.limit = limit

    def get_ids(self, db) -> List[str]:
        """Configured fallback model ids in order (may be empty)."""
        raw = db.get_setting(self.chain_key, None)
        if raw is not None and raw != '':
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return _clean(parsed, self.limit)
            except (ValueError, TypeError):
                pass
        return _clean([db.get_setting(k, '') or '' for k in self.legacy_keys], self.limit)

    def get_models(self, db, require_vision: bool = False) -> List[Dict[str, Any]]:
        """Enabled fallback model rows in order.

        Ids that no longer resolve to a model, or whose model is disabled (or
        lacks vision when required), are skipped so one stale slot does not
        cut the chain short.
        """
        models: List[Dict[str, Any]] = []
        for model_id in self.get_ids(db):
            try:
                model = db.get_model_by_id(model_id)
            except Exception:
                model = None
            if not model or not model.get('enabled', True):
                continue
            if require_vision and not model.get('vision_supported'):
                continue
            models.append(model)
        return models

    def save_ids(self, db, ids) -> List[str]:
        """Persist the chain and its legacy mirrors. Returns the stored list."""
        cleaned = _clean(ids, self.limit)
        db.set_setting(self.chain_key, json.dumps(cleaned))
        for i, key in enumerate(self.legacy_keys):
            db.set_setting(key, cleaned[i] if i < len(cleaned) else '')
        return cleaned


DEFAULT_CHAIN = FallbackChain('default_model_fallback_ids', ['default_model_fallback_id'])
VISION_CHAIN = FallbackChain('vision_fallback_model_ids',
                             ['vision_fallback_model_id', 'vision_fallback_model_2_id'])

# Kept for callers of the default chain.
CHAIN_KEY = DEFAULT_CHAIN.chain_key
LEGACY_KEY = DEFAULT_CHAIN.legacy_keys[0]


def get_fallback_ids(db) -> List[str]:
    """Default-model fallback ids in order (may be empty)."""
    return DEFAULT_CHAIN.get_ids(db)


def get_fallback_models(db) -> List[Dict[str, Any]]:
    """Enabled default-model fallback rows in order."""
    return DEFAULT_CHAIN.get_models(db)


def save_fallback_ids(db, ids) -> List[str]:
    """Persist the default-model fallback chain. Returns it."""
    return DEFAULT_CHAIN.save_ids(db, ids)
