"""What deserves the user's attention on the dashboard.

Pure function so it can be unit-tested without a database: callers pass plain dicts.
Each item: {level: 'warn' | 'danger' | 'info', title, detail, href}
"""

import os
import time
from typing import Any, Callable, Dict, List, Optional

# A schedule that is enabled and more than this many seconds past its next run time is considered stuck.
STALE_SCHEDULE_SECONDS = 15 * 60
LOW_EVAL_SCORE = 0.20
MAX_ITEMS = 6


def _to_epoch(v) -> Optional[float]:
    """next_run_at arrives as an epoch number or an ISO string depending on the trigger."""
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        from datetime import datetime, timezone
        d = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:
        return None


def build_attention(
    agents: List[Dict[str, Any]],
    schedules: List[Dict[str, Any]],
    latest_run: Optional[Dict[str, Any]] = None,
    *,
    workplace_type: Callable[[str], Optional[str]] = lambda _id: None,
    now: Optional[float] = None,
    isdir: Callable[[str], bool] = os.path.isdir,
) -> List[Dict[str, str]]:
    now = time.time() if now is None else now
    items: List[Dict[str, str]] = []

    for a in agents or []:
        if a.get('enabled') in (0, False):
            continue
        ws = (a.get('workspace') or '').strip()
        wp = (a.get('workplace_id') or '').strip()
        if wp and workplace_type(wp) in ('remote', 'tunnel'):
            continue                      # the path lives on another machine
        if ws and not isdir(ws):
            items.append({'level': 'warn', 'title': f"{a.get('name') or a.get('id')}: workspace not found",
                          'detail': ws, 'href': f"/agents/{a.get('id')}"})

    for s in schedules or []:
        if not s.get('enabled'):
            continue
        nxt = _to_epoch(s.get('next_run_at'))
        if nxt is not None and now - nxt > STALE_SCHEDULE_SECONDS:
            mins = int((now - nxt) // 60)
            late = f"{mins} min" if mins < 120 else f"{mins // 60} h"
            items.append({'level': 'warn', 'title': f"Schedule “{s.get('name') or 'Untitled'}” is overdue",
                          'detail': f"Should have run {late} ago", 'href': '/scheduler'})

    max_pts = (latest_run or {}).get('max_points')
    if latest_run and max_pts and latest_run.get('total_points') is not None:
        # attainment is only the alert trigger; the message shows points
        low = latest_run['total_points'] / max_pts < LOW_EVAL_SCORE
    else:
        low = bool(latest_run and latest_run.get('overall_score') is not None
                   and latest_run['overall_score'] < LOW_EVAL_SCORE)
    if low:
        scored = (f"only {latest_run['total_points']:g} points" if max_pts and latest_run.get('total_points') is not None
                  else "low")
        items.append({'level': 'danger', 'title': f"Latest evaluation scored {scored}",
                      'detail': latest_run.get('model_name') or 'Unknown model',
                      'href': f"/history/{latest_run.get('run_id')}"})

    order = {'danger': 0, 'warn': 1, 'info': 2}
    items.sort(key=lambda i: order.get(i['level'], 3))
    return items[:MAX_ITEMS]
