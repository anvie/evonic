"""
Token Monitor Plugin — Flask routes.

- GET /token-monitor                     → dashboard page
- GET /api/token-monitor/snapshot        → consolidated dashboard payload
- GET /api/token-monitor/overview        → headline totals + estimated cost
- GET /api/token-monitor/by-agent        → per-agent breakdown (?rollup=1 folds sub-agents)
- GET /api/token-monitor/by-source       → per-source breakdown
- GET /api/token-monitor/by-model        → per-model breakdown + cost
- GET /api/token-monitor/series          → time-bucketed series (?bucket=hour|day)

All list/overview endpoints accept ?range=24h|7d|30d|all.
"""

import os
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, render_template, request

from plugins.token_monitor.db import usage_db
from plugins.token_monitor import pricing as _pricing

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

_RANGE_HOURS = {'24h': 24, '7d': 24 * 7, '30d': 24 * 30}


def _since_from_request():
    rng = request.args.get('range', '24h')
    hours = _RANGE_HOURS.get(rng)
    if hours is None:
        return None  # 'all' or unknown → no lower bound
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def create_blueprint():
    bp = Blueprint(
        'token_monitor', __name__,
        template_folder=os.path.join(PLUGIN_DIR, 'templates'),
        static_folder=os.path.join(PLUGIN_DIR, 'static'),
        static_url_path='/token-monitor/static',
    )

    @bp.route('/token-monitor')
    def token_monitor_page():
        return render_template('token_monitor.html')

    @bp.route('/api/token-monitor/snapshot')
    def api_snapshot():
        since = _since_from_request()
        rollup = request.args.get('rollup', '').lower() in ('1', 'true')
        requested_bucket = request.args.get('bucket', 'hour')
        bucket = requested_bucket if requested_bucket in ('hour', 'day') else 'hour'
        if request.args.get('range', '24h') == '30d':
            bucket = 'day'
        try:
            agent_limit = int(request.args.get('agent_limit', 30))
        except (TypeError, ValueError):
            agent_limit = 30
        payload = usage_db.snapshot(
            since, rollup_subagents=rollup, bucket=bucket,
            agent_limit=agent_limit)
        pricing = _pricing._load_pricing()
        total_cost = 0.0
        priced = False
        for row in payload['models']:
            row['cost'] = _pricing.cost(
                row.get('key', ''), row.get('prompt_tokens', 0),
                row.get('completion_tokens', 0), pricing)
            if row['cost'] is not None:
                total_cost += row['cost']
                priced = True
        totals = payload['totals']
        totals['billable_input_tokens'] = max(
            totals.get('prompt_tokens', 0) - totals.get('cached_tokens', 0), 0)
        totals['estimated_cost'] = round(total_cost, 4) if priced else None
        return jsonify(payload)

    @bp.route('/api/token-monitor/overview')
    def api_overview():
        since = _since_from_request()
        totals = usage_db.overall_totals(since)
        pricing = _pricing._load_pricing()
        total_cost = 0.0
        priced = False
        for row in usage_db.by_model(since):
            c = _pricing.cost(row.get('key', ''), row.get('prompt_tokens', 0),
                              row.get('completion_tokens', 0), pricing)
            if c is not None:
                total_cost += c
                priced = True
        totals['estimated_cost'] = round(total_cost, 4) if priced else None
        return jsonify(totals)

    @bp.route('/api/token-monitor/by-agent')
    def api_by_agent():
        since = _since_from_request()
        rollup = request.args.get('rollup', '').lower() in ('1', 'true')
        return jsonify({'agents': usage_db.by_agent(since, rollup_subagents=rollup)})

    @bp.route('/api/token-monitor/by-source')
    def api_by_source():
        since = _since_from_request()
        return jsonify({'sources': usage_db.by_source(since)})

    @bp.route('/api/token-monitor/by-model')
    def api_by_model():
        since = _since_from_request()
        pricing = _pricing._load_pricing()
        rows = usage_db.by_model(since)
        for row in rows:
            row['cost'] = _pricing.cost(row.get('key', ''), row.get('prompt_tokens', 0),
                                        row.get('completion_tokens', 0), pricing)
        return jsonify({'models': rows})

    @bp.route('/api/token-monitor/series')
    def api_series():
        since = _since_from_request()
        bucket = request.args.get('bucket', 'hour')
        if bucket not in ('hour', 'day'):
            bucket = 'hour'
        return jsonify({'bucket': bucket, 'series': usage_db.series(since, bucket=bucket)})

    return bp
