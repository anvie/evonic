"""
Token Monitor Plugin — Event handler.

Subscribes to the generic ``llm_usage`` event (emitted by the core LLM client)
and persists one usage row per successful LLM completion.
"""

from datetime import datetime, timedelta, timezone


def _since_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def on_llm_usage(event, sdk):
    """Persist a token usage record from the llm_usage event."""
    try:
        total = int(event.get('total_tokens', 0) or 0)
        if total <= 0:
            return
        from plugins.token_monitor.db import usage_db
        usage_db.record(
            source=event.get('source') or 'other',
            agent_id=event.get('agent_id'),
            agent_name=event.get('agent_name'),
            session_id=event.get('session_id'),
            model=event.get('model'),
            prompt_tokens=int(event.get('prompt_tokens', 0) or 0),
            completion_tokens=int(event.get('completion_tokens', 0) or 0),
            total_tokens=total,
            estimated=bool(event.get('estimated', False)),
            duration_ms=int(event.get('duration_ms', 0) or 0),
            provider=event.get('provider') or None,
            cached_tokens=int(event.get('cached_tokens', 0) or 0),
            reasoning_tokens=int(event.get('reasoning_tokens', 0) or 0),
            usage_details_available=bool(event.get('usage_details_available', False)),
        )
    except Exception as e:
        try:
            sdk.log(f"failed to record usage: {e}", 'error')
        except Exception:
            pass


def dashboard_usage_card(sdk):
    """Dashboard card: token usage and estimated cost in the last 24h."""
    try:
        from plugins.token_monitor.db import usage_db
        from plugins.token_monitor import pricing as _pricing

        since = _since_iso(24)
        totals = usage_db.overall_totals(since)
        by_model = usage_db.by_model(since)

        pricing = _pricing._load_pricing()
        total_cost = 0.0
        priced = False
        for row in by_model:
            c = _pricing.cost(row.get('key', ''), row.get('prompt_tokens', 0),
                              row.get('completion_tokens', 0), pricing)
            if c is not None:
                total_cost += c
                priced = True

        total_tokens = totals.get('total_tokens', 0)
        cost_label = f"${total_cost:,.2f}" if priced else "n/a"
        return {
            'id': 'token_monitor_usage',
            'title': 'Token Usage (24h)',
            'link': '/token-monitor',
            'feature_card': {
                'count': f"{total_tokens:,}",
                'detail': f"{totals.get('calls', 0)} calls · est. {cost_label}",
                'border_color': 'indigo',
                'bg_color': 'indigo',
                'icon_color': 'indigo',
            },
        }
    except Exception:
        return None
