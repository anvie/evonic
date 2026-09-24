"""
Ask User Question Plugin — Flask Route Handlers

Provides:
- POST /api/ask_user/answer   → submit answers for a pending question
- POST /api/ask_user/cancel   → close a pending question without answering
- GET  /api/ask_user/pending  → questions waiting in a session (?session_id=),
                                plus how any ?known= ids that stopped waiting ended
- GET  /ask_user/static/...   → the plugin's web UI (ask_user.js / ask_user.css)

The web UI is self-contained: an after_app_request hook adds the plugin's
<script>/<link> to agent pages, so no Evonic core template or JS changes are
needed. The /api/ routes sit behind the app's session auth and CSRF checks.
"""

import os
import re

from flask import Blueprint, jsonify, request

from plugins.ask_user.store import question_registry

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(PLUGIN_DIR, 'static')
STATIC_URL = '/ask_user/static'

# Agent detail page (the web chat); /agents/<id> with optional trailing slash.
_AGENT_PAGE = re.compile(r'^/agents/[^/]+/?$')


def _asset_tags() -> str:
    """<link>/<script> for the web UI, cache-busted by file mtime."""
    def url(name):
        try:
            version = int(os.path.getmtime(os.path.join(STATIC_DIR, name)))
        except OSError:
            version = 0
        return f'{STATIC_URL}/{name}?v={version}'
    return (f'<link rel="stylesheet" href="{url("ask_user.css")}">'
            f'<script src="{url("ask_user.js")}" defer></script>')


def _plugin_enabled() -> bool:
    try:
        from models.db import db
        return db.get_setting('plugin_enabled:ask_user') == '1'
    except Exception:
        return False


def inject_assets(response):
    """Add the plugin's UI to agent pages (runs after every app request)."""
    if (request.method != 'GET' or response.status_code != 200
            or response.mimetype != 'text/html' or response.direct_passthrough
            or not _AGENT_PAGE.match(request.path) or not _plugin_enabled()):
        return response
    html = response.get_data(as_text=True)
    marker = html.rfind('</body>')
    if marker == -1 or f'{STATIC_URL}/ask_user.js' in html:
        return response
    response.set_data(html[:marker] + _asset_tags() + html[marker:])
    return response


def create_blueprint():
    bp = Blueprint('ask_user', __name__, static_folder=STATIC_DIR, static_url_path=STATIC_URL)
    bp.after_app_request(inject_assets)

    @bp.route('/api/ask_user/answer', methods=['POST'])
    def answer_question():
        data = request.get_json(silent=True) or {}
        question_id = str(data.get('question_id') or '')
        if not question_id:
            return jsonify({'error': 'question_id is required.'}), 400
        pending, error = question_registry.answer(question_id, data.get('answers'))
        if error:
            status = 404 if question_registry.get(question_id) is None else 400
            return jsonify({'error': error}), status
        return jsonify({'ok': True, 'answers': pending.answers})

    @bp.route('/api/ask_user/cancel', methods=['POST'])
    def cancel_question():
        data = request.get_json(silent=True) or {}
        question_id = str(data.get('question_id') or '')
        if not question_id:
            return jsonify({'error': 'question_id is required.'}), 400
        _, error = question_registry.dismiss(question_id)
        if error:
            status = 404 if question_registry.get(question_id) is None else 400
            return jsonify({'error': error}), status
        return jsonify({'ok': True})

    @bp.route('/api/ask_user/pending', methods=['GET'])
    def pending_questions():
        session_id = request.args.get('session_id', '')
        if not session_id:
            return jsonify({'error': 'session_id is required.'}), 400
        pending = question_registry.pending_for_session(session_id)
        pending_ids = {pq.question_id for pq in pending}
        # Ids the UI is still showing but that stopped waiting: report how they ended
        # ('expired' when the server no longer knows them, e.g. after a restart).
        resolved = {}
        for qid in filter(None, request.args.get('known', '').split(',')[:20]):
            if qid not in pending_ids:
                in_flight = question_registry.get(qid)
                if in_flight is None:
                    resolved[qid] = question_registry.resolved_status(qid) or 'expired'
        return jsonify({
            'questions': [
                {'question_id': pq.question_id, 'agent_id': pq.agent_id,
                 'questions': pq.questions, 'created_at': pq.created_at}
                for pq in pending
            ],
            'resolved': resolved,
        })

    return bp
