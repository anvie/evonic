"""
CMP compactor — interface-preserving path cards, deterministic layer.

Card prose is maintained incrementally by the per-turn single-pass op
(detector envelope → store.apply_card_delta), so suspension no longer needs
an LLM summarization call. What remains here is only deterministic:
mechanically filling gaps from the transcript, and token estimates. Node identity is immutable — finalization
never touches a path's title/action (renaming established map nodes was a
live bug of the LLM-card era).
"""
from __future__ import annotations

import json
import logging

_logger = logging.getLogger(__name__)


def collect_path_entries(chatlog, path: dict) -> list:
    """All chatlog entries belonging to the path's segments (ts ranges)."""
    entries = []
    for start, end in path.get('segments') or []:
        try:
            if end is None:
                entries.extend(chatlog.get_entries_after_ts(start))
            else:
                entries.extend(chatlog.get_entries_between_ts(start, end))
        except Exception:
            _logger.exception("CMP: failed reading chatlog segment [%s, %s]",
                              start, end)
    return entries


def path_token_estimate(chatlog, path: dict) -> int:
    """Estimated token size of a path's transcript (its chatlog segments) —
    what it costs / would cost in the context window. Best-effort, 0 on
    failure."""
    try:
        from backend.llm_usage_events import estimate_tokens
        total = 0
        for entry in collect_path_entries(chatlog, path):
            total += estimate_tokens(str(entry.get('content') or ''))
            params = entry.get('params')
            if params:
                total += estimate_tokens(json.dumps(params, default=str))
        return total
    except Exception:
        _logger.warning("CMP path token estimate failed", exc_info=True)
        return 0


def path_llm_token_estimate(chatlog, path: dict) -> int:
    """Token size of the path's transcript AS THE LLM ACTUALLY SEES IT —
    segment-scoped reconstruction with the same tool-output compaction and
    closed-segment tail trimming the assembler applies. The gap between this
    and path_token_estimate (the raw transcript) is what compaction saves.
    Best-effort, 0 on failure."""
    try:
        from backend.llm_usage_events import estimate_tokens
        total = 0
        for msg in chatlog.get_entries_for_llm_segments(
                path.get('segments') or []):
            total += estimate_tokens(str(msg.get('content') or ''))
            for tc in msg.get('tool_calls') or []:
                total += estimate_tokens(
                    str((tc.get('function') or {}).get('arguments') or ''))
        return total
    except Exception:
        _logger.warning("CMP path LLM token estimate failed", exc_info=True)
        return 0


def card_token_estimate(path: dict) -> int:
    """Estimated token size of a path's IPPC card — the compact structured
    summary that stays in the context window when the path is offloaded."""
    try:
        from backend.llm_usage_events import estimate_tokens
        parts = [path.get('title'), path.get('goal'), path.get('outcome')]
        parts.extend(path.get('key_facts') or [])
        parts.extend(path.get('artifacts') or [])
        return estimate_tokens('\n'.join(str(p) for p in parts if p))
    except Exception:
        return 0


def finalize_active_card(chatlog, cmp: dict, ms) -> None:
    """Deterministic finalization of the ACTIVE path's card before it is
    suspended (switch/branch). Card prose is kept fresh by the per-turn op;
    this only mechanically fills an empty outcome from the transcript. Never touches title/action (node identity is immutable
    after creation). Never raises."""
    try:
        from backend.agent_runtime.cmp.store import OUTCOME_MAX
        path = cmp['paths'][cmp['active_id']]
        if not path.get('card_stale', True):
            return
        if not path.get('outcome'):
            entries = collect_path_entries(chatlog, path)
            last_final = next((e.get('content', '') for e in reversed(entries)
                               if e.get('type') in ('final', 'intermediate')), '')
            path['outcome'] = str(last_final)[:OUTCOME_MAX]
        path['card_stale'] = False
    except Exception:
        _logger.exception("CMP finalize_active_card failed — switch proceeds")
