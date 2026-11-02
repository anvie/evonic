"""
Ask User Question — Flask route handlers for the web chat's question card.

Provides:
- POST /api/ask_user/answer   → submit answers for a pending question
- POST /api/ask_user/cancel   → close a pending question without answering
- GET  /api/ask_user/pending  → questions waiting in a session (?session_id=),
                                plus how any ?known= ids that stopped waiting ended

The card itself is static/js/ask-user.js, loaded by the agent page. The /api/
routes sit behind the app's session auth and CSRF checks.
"""

from flask import Blueprint, jsonify, request

from backend.ask_user_store import question_registry

ask_user_bp = Blueprint('ask_user', __name__)


@ask_user_bp.route('/api/ask_user/answer', methods=['POST'])
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


@ask_user_bp.route('/api/ask_user/cancel', methods=['POST'])
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


@ask_user_bp.route('/api/ask_user/pending', methods=['GET'])
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
